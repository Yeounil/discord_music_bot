import discord
from discord.ext import commands
from discord import app_commands
import yt_dlp
import asyncio
import datetime
from concurrent.futures import ThreadPoolExecutor

FFMPEG_PATH = "C:/Tools/ffmpeg/bin/ffmpeg.exe"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"

def is_youtube_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return "youtube.com" in u or "youtu.be" in u


class MusicBot(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # 길드별 상태
        self.voice_clients: dict[int, discord.VoiceClient] = {}
        self.queues: dict[int, list[dict]] = {}
        self.is_playing: dict[int, bool] = {}
        self.current_songs: dict[int, dict | None] = {}

        # UI/활동/유휴/프리로드 관리
        self.last_message: dict[int, discord.Message] = {}
        self.last_activity: dict[int, datetime.datetime] = {}
        self.inactivity_tasks: dict[int, asyncio.Task] = {}
        self.ui_update_tasks: dict[int, asyncio.Task] = {}

        # 프리로드: 다음 곡 오디오 소스를 미리 만들어 둠
        self.preload_tasks: dict[int, asyncio.Task] = {}
        self.preloaded_sources: dict[int, tuple[str, discord.AudioSource]] = {}  # key: guild_id -> (keystr, source)

        # yt-dlp 검색 (블로킹) → 스레드 풀에 맡김
        self.search_executor = ThreadPoolExecutor(max_workers=1)  # 대기열 추가 시 안정성

    def cog_unload(self):
        self.search_executor.shutdown(wait=False)
        # 남아있는 프리로드 태스크 정리
        for t in self.preload_tasks.values():
            t.cancel()

    # ---------- 공용 유틸 ----------
    def update_activity(self, guild_id: int):
        self.last_activity[guild_id] = datetime.datetime.utcnow()

    async def start_inactivity_timer(self, guild_id: int, interaction: discord.Interaction):
        if task := self.inactivity_tasks.get(guild_id):
            task.cancel()

        async def timer():
            try:
                await asyncio.sleep(600)  # 10분
            except asyncio.CancelledError:
                return
            last_input = self.last_activity.get(guild_id)
            if (
                not self.is_playing.get(guild_id, False)
                and last_input
                and (datetime.datetime.utcnow() - last_input).total_seconds() >= 600
            ):
                await self.disconnect_and_cleanup(guild_id, interaction)

        self.inactivity_tasks[guild_id] = asyncio.create_task(timer())

    async def disconnect_and_cleanup(self, guild_id: int, interaction: discord.Interaction | None):
        # 프리로드 리소스/태스크도 정리
        if t := self.preload_tasks.get(guild_id):
            t.cancel()
        self.preload_tasks.pop(guild_id, None)
        self.preloaded_sources.pop(guild_id, None)

        vc = self.voice_clients.get(guild_id)
        if vc and vc.is_connected():
            try:
                await vc.disconnect()
            except Exception:
                pass
        self.voice_clients.pop(guild_id, None)
        self.queues.pop(guild_id, None)
        self.is_playing[guild_id] = False
        self.current_songs[guild_id] = None
        await self.delete_player_ui(guild_id)

    # ---------- 음성 연결 ----------
    async def join_voice_channel(self, interaction: discord.Interaction) -> discord.VoiceClient | None:
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("🚫 길드 컨텍스트에서만 사용할 수 있어요.", ephemeral=True)
            return None

        if interaction.user is None or interaction.user.voice is None:
            await interaction.followup.send("🚫 먼저 음성 채널에 들어가 주세요!", ephemeral=True)
            return None

        target_channel = interaction.user.voice.channel
        vc = self.voice_clients.get(guild.id)

        if vc is None or not vc.is_connected():
            vc = await target_channel.connect()
            self.voice_clients[guild.id] = vc
            return vc

        if getattr(vc, "channel", None) != target_channel:
            await vc.move_to(target_channel)
        return vc

    # ---------- 검색 ----------
    def search_youtube_blocking(self, query: str) -> dict | None:
        ydl_opts = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "default_search": "ytsearch",
            "cookiefile": "cookies.txt",  # 없으면 yt-dlp가 무시
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                target = query if is_youtube_url(query) else f"ytsearch:{query}"
                info = ydl.extract_info(target, download=False)
                if not info:
                    return None
                if "entries" in info:
                    entries = info.get("entries") or []
                    if not entries:
                        return None
                    info = entries[0]

                return {
                    "url": info.get("url"),
                    "webpage_url": info.get("webpage_url") or info.get("original_url"),
                    "title": info.get("title") or "(제목 없음)",
                    "thumbnail": info.get("thumbnail", ""),
                    "http_headers": info.get("http_headers") or {},  # ffmpeg용 헤더
                }
        except Exception:
            return None

    async def search_youtube_async(self, query: str) -> dict | None:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.search_executor, self.search_youtube_blocking, query)

    # ---------- FFmpeg 헤더/옵션 ----------
    def _headers_to_beforeopt(self, headers: dict, referer: str | None = None) -> str:
        """
        ffmpeg -headers 인자에 넣을 원시 헤더(진짜 CRLF, 마지막 CRLF 포함) + UA/Origin/Referer 보강
        """
        add = {
            "User-Agent": UA,
            "Origin": "https://www.youtube.com",
        }
        if referer:
            add["Referer"] = referer
        merged = {**headers, **add}
        raw = "\r\n".join(f"{k}: {v}" for k, v in merged.items()) + "\r\n"   # 실제 CRLF
        return f'-headers "{raw}" '

    def _make_ffmpeg_opts(
        self,
        headers: dict,
        use_filter: bool,
        for_pcm: bool = False,
        referer: str | None = None,
    ) -> dict:
        before = (
            self._headers_to_beforeopt(headers, referer=referer) +
            "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
            "-rw_timeout 30000000 "
            "-nostdin -fflags +genpts -probesize 32M -analyzeduration 10M "
            "-thread_queue_size 2048 "
            f'-user_agent "{UA}" '
        )
        # 공통 옵션 (Opus 경로에는 -ar/-ac 넣지 않음 → 중복 경고 방지)
        opts = "-vn -bufsize 8M -loglevel warning"
        if use_filter:
            opts = "-vn -af aresample=async=1:min_hard_comp=0.100:first_pts=0 -bufsize 8M -loglevel warning"
        if for_pcm:
            # PCM에서만 표준화
            opts = "-vn -ar 48000 -ac 2 -bufsize 8M -loglevel warning"
        return {"before_options": before, "options": opts, "executable": FFMPEG_PATH}

    # ---------- 오디오 소스 생성(재시도) ----------
    async def _create_source(self, url: str, headers: dict, referer: str | None):
        # 1차: Opus 재인코딩 + aresample 필터
        try:
            return await discord.FFmpegOpusAudio.from_probe(
                url, codec="libopus", bitrate=128,
                **self._make_ffmpeg_opts(headers, use_filter=True, for_pcm=False, referer=referer)
            )
        except Exception:
            pass
        # 2차: Opus 재인코딩(필터 제거)
        try:
            return await discord.FFmpegOpusAudio.from_probe(
                url, codec="libopus", bitrate=128,
                **self._make_ffmpeg_opts(headers, use_filter=False, for_pcm=False, referer=referer)
            )
        except Exception:
            pass
        # 3차: PCM (최후 수단, 스트리밍)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: discord.FFmpegPCMAudio(
                url, **self._make_ffmpeg_opts(headers, use_filter=False, for_pcm=True, referer=referer)
            )
        )

    async def create_audio_source_async(self, song: dict) -> discord.AudioSource:
        url   = song["url"]
        hdrs  = song.get("http_headers") or {}
        refer = song.get("webpage_url") or None
        return await self._create_source(url, hdrs, refer)

    # ---------- 프리로드(선로딩) ----------
    def _song_key(self, song: dict) -> str:
        """프리로드 캐시 키(만료 최소화 위해 URL보다 webpage_url/제목 위주)"""
        return song.get("webpage_url") or song.get("url") or song.get("title") or ""

    def _cancel_preload(self, guild_id: int):
        if t := self.preload_tasks.get(guild_id):
            t.cancel()
        self.preload_tasks.pop(guild_id, None)
        self.preloaded_sources.pop(guild_id, None)

    def _store_preloaded(self, guild_id: int, song: dict, source: discord.AudioSource):
        key = self._song_key(song)
        self.preloaded_sources[guild_id] = (key, source)

    def _get_preloaded(self, guild_id: int, song: dict):
        key = self._song_key(song)
        stored = self.preloaded_sources.get(guild_id)
        if stored and stored[0] == key:
            return stored[1]
        return None

    def _schedule_preload_next(self, interaction: discord.Interaction, delay: float = 0.8):
        """대기열 맨 앞 곡을 일정 지연 후 프리로드. 기존 태스크가 있으면 취소."""
        guild_id = interaction.guild.id
        # 기존 프리로드 태스크가 있으면 취소
        if t := self.preload_tasks.get(guild_id):
            t.cancel()

        async def _task():
            try:
                await asyncio.sleep(delay)  # 재생 시작 직후/대기열 추가 직후 스파이크를 피해 약간 뒤에 수행
                queue = self.queues.get(guild_id, [])
                if not queue:
                    return
                next_song = queue[0]
                # 이미 프리로드 되어 있으면 스킵
                if self._get_preloaded(guild_id, next_song):
                    return
                # 소스 생성
                src = await self.create_audio_source_async(next_song)
                # 저장
                self._store_preloaded(guild_id, next_song, src)
            except asyncio.CancelledError:
                return
            except Exception:
                # 프리로드 실패는 조용히 무시(실재생 시 재시도)
                pass

        self.preload_tasks[guild_id] = asyncio.create_task(_task())

    # ---------- 재생/대기열 ----------
    async def play_music(self, interaction: discord.Interaction, song: dict) -> bool:
        guild_id = interaction.guild.id
        vc = self.voice_clients.get(guild_id)
        if vc is None:
            vc = await self.join_voice_channel(interaction)
            if vc is None:
                self.is_playing[guild_id] = False
                return False

        try:
            # 1순위: 프리로드된 소스가 있으면 재사용
            source = self._get_preloaded(guild_id, song)
            if source is None:
                source = await self.create_audio_source_async(song)
            else:
                # 사용했으니 캐시 비움
                self.preloaded_sources.pop(guild_id, None)
        except Exception:
            await interaction.followup.send("⚠️ 오디오 소스를 만들 수 없었어요. 다른 곡을 시도해 주세요.", ephemeral=True)
            await self.play_next(interaction)
            return False

        self.current_songs[guild_id] = song

        def _after_playback(error: Exception | None):
            fut = asyncio.run_coroutine_threadsafe(self.play_next(interaction), self.bot.loop)
            try:
                fut.result()
            except Exception:
                pass

        try:
            vc.play(source, after=_after_playback)
        except Exception:
            await interaction.followup.send("⚠️ 재생을 시작할 수 없었어요.", ephemeral=True)
            await self.play_next(interaction)
            return False

        # UI 갱신
        await self.schedule_ui_update(interaction, delay=0.25)
        # 다음 곡 프리로드 예약
        self._schedule_preload_next(interaction, delay=0.8)
        return True

    async def play_next(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        queue = self.queues.get(guild_id, [])
        if queue:
            next_song = queue.pop(0)
            # 다음 곡 재생 → 그 다음 곡 프리로드 태스크는 새로 잡을 것
            await self.play_music(interaction, next_song)
        else:
            self.is_playing[guild_id] = False
            self.current_songs[guild_id] = None
            # 프리로드 리소스/태스크 정리
            self._cancel_preload(guild_id)
            await self.delete_player_ui(guild_id)
            await self.start_inactivity_timer(guild_id, interaction)

    # ---------- UI ----------
    async def schedule_ui_update(self, interaction: discord.Interaction, delay: float = 0.3):
        guild_id = interaction.guild.id
        if task := self.ui_update_tasks.get(guild_id):
            task.cancel()

        async def _task():
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            await self.send_player_ui(interaction)

        self.ui_update_tasks[guild_id] = asyncio.create_task(_task())

    async def send_player_ui(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        song = self.current_songs.get(guild_id)
        if not song:
            return
        embed = discord.Embed(
            title="🎵 현재 재생 중",
            description=f"[{song['title']}]({song['webpage_url']})",
        )
        if thumb := song.get("thumbnail"):
            embed.set_thumbnail(url=thumb)

        view = PlayerView(self, guild_id)

        msg = self.last_message.get(guild_id)
        if msg:
            try:
                await msg.edit(embed=embed, view=view)
                return
            except (discord.NotFound, discord.Forbidden):
                pass

        channel = interaction.channel
        if channel is None:
            return
        try:
            sent = await channel.send(embed=embed, view=view)
            self.last_message[guild_id] = sent
        except discord.Forbidden:
            pass

    async def delete_player_ui(self, guild_id: int):
        if msg := self.last_message.get(guild_id):
            try:
                await msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
            finally:
                self.last_message.pop(guild_id, None)

    # ---------- Slash Commands ----------
    @app_commands.command(name="play", description="노래를 재생합니다.")
    async def play(self, interaction: discord.Interaction, *, query: str):
        await interaction.response.defer()  # 3초 ACK

        if interaction.guild is None:
            await interaction.followup.send("🚫 길드에서만 사용할 수 있어요.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        self.update_activity(guild_id)

        vc = await self.join_voice_channel(interaction)
        if vc is None:
            return

        song = await self.search_youtube_async(query)
        if not song or not song.get("url"):
            await interaction.followup.send("🔍 검색 결과가 없어요. 다른 키워드/URL을 시도해 주세요.", ephemeral=True)
            return

        q = self.queues.setdefault(guild_id, [])
        if self.is_playing.get(guild_id, False) and vc.is_playing():
            q.append(song)
            await interaction.followup.send(f"📥 **{song['title']}** 대기열에 추가됨!")
            # 대기열이 이제 (막 추가되어) 1개가 됐다면 바로 프리로드 스케줄
            if len(q) == 1:
                self._schedule_preload_next(interaction, delay=0.6)
        else:
            self.is_playing[guild_id] = True
            ok = await self.play_music(interaction, song)
            if ok:
                await interaction.followup.send(f"▶️ **{song['title']}** 재생 시작!")
            # 실패하면 play_music 내부에서 처리

    @app_commands.command(name="queue", description="대기열 보기")
    async def queue(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send("🚫 길드에서만 사용할 수 있어요.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        self.update_activity(guild_id)
        queue = self.queues.get(guild_id, [])
        if not queue:
            await interaction.followup.send("⛔ 대기열이 비어 있어요!", ephemeral=True)
        else:
            msg = "\n".join([f"{i+1}. {s['title']}" for i, s in enumerate(queue)])
            await interaction.followup.send(f"📜 대기열:\n{msg}", ephemeral=True)

    @app_commands.command(name="skip", description="다음 곡으로")
    async def skip(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send("🚫 길드에서만 사용할 수 있어요.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        self.update_activity(guild_id)
        vc = self.voice_clients.get(guild_id)
        if vc and (vc.is_playing() or vc.is_paused()):
            try:
                vc.stop()  # after 콜백을 통해 다음 곡 진행
            except Exception:
                pass
            # 스킵하면 기존 프리로드는 더 이상 유효하지 않을 수 있으니 취소
            self._cancel_preload(guild_id)
            await interaction.followup.send("⏭️ 스킵합니다.", ephemeral=True)
        else:
            await interaction.followup.send("⛔ 현재 재생 중인 곡이 없어요.", ephemeral=True)

    @app_commands.command(name="stop", description="중지 및 퇴장")
    async def stop(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if interaction.guild is None:
            await interaction.followup.send("🚫 길드에서만 사용할 수 있어요.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        self.update_activity(guild_id)

        vc = self.voice_clients.get(guild_id)
        if vc and (vc.is_playing() or vc.is_paused()):
            try:
                vc.stop()
            except Exception:
                pass
            await asyncio.sleep(0.1)

        await self.disconnect_and_cleanup(guild_id, interaction)
        await interaction.followup.send("🛑 정지 및 나감", ephemeral=True)


class PlayerView(discord.ui.View):
    def __init__(self, music_bot: MusicBot, guild_id: int):
        super().__init__(timeout=None)
        self.music_bot = music_bot
        self.guild_id = guild_id

    @discord.ui.button(label="⏯️ 재생/일시정지", style=discord.ButtonStyle.primary, custom_id="player:toggle")
    async def toggle_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        vc = self.music_bot.voice_clients.get(self.guild_id)
        self.music_bot.update_activity(self.guild_id)
        if not vc:
            await interaction.followup.send("⛔ 보이스 연결이 없어요.", ephemeral=True)
            return
        if vc.is_playing():
            vc.pause()
            await interaction.followup.send("⏸️ 일시정지", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            await interaction.followup.send("▶️ 재생", ephemeral=True)
        else:
            await interaction.followup.send("⛔ 재생 중이 아니에요.", ephemeral=True)

    @discord.ui.button(label="⏭️ 다음 곡", style=discord.ButtonStyle.secondary, custom_id="player:next")
    async def next_song(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        vc = self.music_bot.voice_clients.get(self.guild_id)
        self.music_bot.update_activity(self.guild_id)
        if vc and (vc.is_playing() or vc.is_paused()):
            try:
                vc.stop()
            except Exception:
                pass
            # 프리로드 취소
            self.music_bot._cancel_preload(self.guild_id)
            await interaction.followup.send("⏭️ 다음 곡으로 이동합니다.", ephemeral=True)
        else:
            await interaction.followup.send("⛔ 재생 중이 아니에요.", ephemeral=True)

    @discord.ui.button(label="📃 대기열 출력", style=discord.ButtonStyle.success, custom_id="player:queue")
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        queue = self.music_bot.queues.get(self.guild_id, [])
        self.music_bot.update_activity(self.guild_id)
        if not queue:
            await interaction.followup.send("⛔ 대기열이 비어 있어요!", ephemeral=True)
        else:
            msg = "\n".join([f"{i+1}. {s['title']}" for i, s in enumerate(queue)])
            await interaction.followup.send(f"📜 대기열:\n{msg}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicBot(bot))
