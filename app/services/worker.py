import asyncio
import os
import time

import requests
from moviepy.editor import AudioFileClip, VideoFileClip
from yt_dlp import YoutubeDL

from app.config import (
    DOWNLOAD_STALL_CHECK_INTERVAL_SECONDS,
    DOWNLOAD_STALL_TIMEOUT_SECONDS,
    EXTERNAL_UPLOAD_TIMEOUT_SECONDS,
    YTDLP_FRAGMENT_RETRIES,
    YTDLP_RETRIES,
    YTDLP_SOCKET_TIMEOUT,
)
from app.errors import (
    ERR_TIMEOUT,
    ERR_WORKER_CANCELLED,
    ERR_WORKER_FILE_NOT_FOUND,
    ERR_WORKER_STALLED,
    ERR_WORKER_TRIM_FAILED,
    ERR_WORKER_TRIM_RANGE_INVALID,
    ERR_WORKER_UPLOAD_BAD_RESPONSE,
    ERR_WORKER_UPLOAD_FAILED,
    ERR_WORKER_UPLOAD_HTTP,
    ERR_UNKNOWN,
    ERR_WORKER_RUNTIME,
    WorkerCancelledError,
)
from app.i18n import t
from app.jobs import request_active_download_cancel, safe_filename
from app.logging_utils import classify_exception_error_code, log_event, worker_error
from app.settings_store import get_user_settings_sync
from app.state import JOB_PROGRESS
def _sync_worker(url, tmpdir, platform, yt_type, start, end, ffmpeg_path, user_id, loop, progress_q, cancel_event=None, cancel_reason_ref=None):
    def _cancel_reason():
        if isinstance(cancel_reason_ref, list) and cancel_reason_ref:
            return cancel_reason_ref[0]
        return None

    def _raise_if_cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise WorkerCancelledError(_cancel_reason() or "cancelled")

    def _send_progress(payload):
        try:
            _raise_if_cancelled()
            loop.call_soon_threadsafe(progress_q.put_nowait, payload)
        except WorkerCancelledError:
            raise
        except Exception:
            pass

    def _sleep_backoff(attempt):
        wait_seconds = min(5, attempt)
        deadline = time.time() + wait_seconds
        while True:
            _raise_if_cancelled()
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            time.sleep(min(0.25, remaining))

    def _progress_hook(d):
        try:
            _raise_if_cancelled()
            payload = {
                'status': d.get('status'),
                'total': d.get('total_bytes') or d.get('total_bytes_estimate'),
                'downloaded': d.get('downloaded_bytes') or 0,
                'speed': d.get('speed'),
                'eta': d.get('eta')
            }
            _send_progress(payload)
        except WorkerCancelledError:
            raise
        except Exception:
            pass

    try:
        _raise_if_cancelled()
        # Синхронно читаем пользовательские настройки качества
        s = get_user_settings_sync(user_id)
        quality_pref = s.get("quality", {}).get(platform, "best")

        ext = "mp3" if (platform == "soundcloud" or yt_type == "audio") else "mp4"

        # Выбираем формат загрузки в зависимости от yt_type и quality_pref
        if yt_type == "audio":
            ydl_format = 'bestaudio/best'
        else:
            # Видео: предпочитаем mp4/m4a, иначе fallback и конвертация в mp4
            if quality_pref == "720":
                ydl_format = 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/bestvideo[height<=720]+bestaudio/best[height<=720]/best'
            elif quality_pref == "480":
                ydl_format = 'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/bestvideo[height<=480]+bestaudio/best[height<=480]/best'
            else:
                ydl_format = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo+bestaudio/best'

        ydl_opts = {
            'format': ydl_format,
            'ffmpeg_location': ffmpeg_path,
            'outtmpl': os.path.join(tmpdir, '%(id)s.%(ext)s'),
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': YTDLP_SOCKET_TIMEOUT,
            'retries': YTDLP_RETRIES,
            'fragment_retries': YTDLP_FRAGMENT_RETRIES,
            'continuedl': True,
            'progress_hooks': [_progress_hook],
        }

        # Постобработка аудио (извлечение в mp3) с маппингом quality -> bitrate
        if yt_type == "audio":
            preferredquality = "320"
            if quality_pref in ("128", "320"):
                preferredquality = quality_pref
            # Если quality_pref == 'best', оставляем 320 как дефолт высокого качества
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': preferredquality,
            }]
        else:
            # Гарантируем итоговый контейнер mp4
            ydl_opts['merge_output_format'] = 'mp4'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }]

        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        _raise_if_cancelled()

        media_id = info.get('id') or 'media'
        title = info.get('title', 'Media')
        uploader = info.get('uploader', 'Unknown')

        # Надёжно определяем итоговый путь файла (postprocessor может менять расширение)
        candidates = []
        def add_candidate(p):
            if p and p not in candidates:
                candidates.append(p)

        requested = info.get("requested_downloads") or []
        for d in requested:
            add_candidate(d.get("filepath"))
            add_candidate(d.get("_filename"))

        add_candidate(info.get("filepath"))
        add_candidate(info.get("_filename"))
        try:
            add_candidate(ydl.prepare_filename(info))
        except Exception:
            pass

        add_candidate(os.path.join(tmpdir, f"{media_id}.{ext}"))

        ext_suffix = f".{ext}"
        for p in list(candidates):
            try:
                root, _ = os.path.splitext(p)
                add_candidate(root + ext_suffix)
            except Exception:
                pass

        file_path = next((p for p in candidates if p and os.path.exists(p)), None)
        if not file_path:
            matches = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir) if f.lower().endswith(ext_suffix)]
            if matches:
                matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                file_path = matches[0]

        if not file_path or not os.path.exists(file_path):
            return worker_error(ERR_WORKER_FILE_NOT_FOUND, f"{ext} файл не найден после скачивания.")

        # Выполняем обрезку, если задан диапазон
        if start is not None and end is not None:
            _raise_if_cancelled()
            trimmed_path = os.path.join(tmpdir, f"trimmed.{ext}")
            try:
                if ext == "mp3":
                    clip = None
                    subclip = None
                    try:
                        clip = AudioFileClip(file_path)
                        subclip = clip.subclip(start, end)
                        subclip.write_audiofile(trimmed_path, bitrate="192k", logger=None)
                    finally:
                        try:
                            if subclip:
                                subclip.close()
                        except Exception:
                            pass
                        try:
                            if clip:
                                clip.close()
                        except Exception:
                            pass
                else:
                    clip = None
                    subclip = None
                    try:
                        clip = VideoFileClip(file_path)
                        subclip = clip.subclip(start, end)
                        subclip.write_videofile(trimmed_path, codec="libx264", audio_codec="aac", logger=None)
                    finally:
                        try:
                            if subclip:
                                subclip.close()
                        except Exception:
                            pass
                        try:
                            if clip:
                                clip.close()
                        except Exception:
                            pass
                file_path = trimmed_path
                _raise_if_cancelled()
            except Exception as e:
                try:
                    if ext == "mp3":
                        with AudioFileClip(file_path) as ci:
                            duration = int(ci.duration)
                    else:
                        with VideoFileClip(file_path) as ci:
                            duration = int(ci.duration)
                except Exception:
                    duration = None
                if duration:
                    return worker_error(ERR_WORKER_TRIM_RANGE_INVALID, f"Не могу обрезать: неверный диапазон. Длительность: {duration} сек.")
                return worker_error(ERR_WORKER_TRIM_FAILED, f"Не могу обрезать файл: {e}")

        # Выбираем: отправить файл в Telegram или загрузить на gofile
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if size_mb <= 50:
            return {'status': 'ok', 'mode': 'file', 'file_path': file_path, 'ext': ext, 'title': title, 'uploader': uploader}
        else:
            GOFILE_SERVERS = [
                "https://store1.gofile.io/uploadFile",
                "https://store2.gofile.io/uploadFile",
                "https://store3.gofile.io/uploadFile",
            ]
            last_error = None
            for server in GOFILE_SERVERS:
                _raise_if_cancelled()
                for attempt in range(1, 4):
                    _raise_if_cancelled()
                    try:
                        _send_progress({
                            'phase': 'uploading',
                            'percent': 90,
                            'status': 'uploading',
                            'server': server,
                            'attempt': attempt
                        })
            
                        with open(file_path, 'rb') as f:
                            safe_title = safe_filename(title)
                            files = {'file': (f"{safe_title}.{ext}", f)}
                            r = requests.post(server, files=files, timeout=EXTERNAL_UPLOAD_TIMEOUT_SECONDS)
                        _raise_if_cancelled()
                        if r.status_code == 429:
                            last_error = f"rate limited ({r.status_code})"
                            _sleep_backoff(attempt)
                            continue
                        if r.status_code >= 500:
                            if "not enough server space" in (r.text or "").lower():
                                last_error = "not enough server space"
                                break
                            last_error = f"server error ({r.status_code})"
                            _sleep_backoff(attempt)
                            break
                        if r.status_code < 200 or r.status_code >= 300:
                            last_error = f"http {r.status_code}"
                            _sleep_backoff(attempt)
                            continue
                        try:
                            data = r.json()
                        except Exception:
                            last_error = f"non-json response ({r.status_code})"
                            _sleep_backoff(attempt)
                            continue
                        if data.get("status") == "ok":
                            link = (data.get("data") or {}).get("downloadPage")
                            if link:
                                _send_progress({
                                    'phase': 'uploaded',
                                    'percent': 100,
                                    'status': 'uploaded',
                                    'server': server
                                })
                                return {'status': 'ok', 'mode': 'link', 'link': link, 'title': title, 'uploader': uploader}
                            last_error = f"bad response: {data}"
                            _sleep_backoff(attempt)
                            continue
                        else:
                            if "not enough server space" in (r.text or "").lower():
                                last_error = "not enough server space"
                                break
                            last_error = str(data)
                            _sleep_backoff(attempt)
                    except WorkerCancelledError:
                        raise
                    except Exception as e:
                        last_error = str(e)
                        _sleep_backoff(attempt)
                if last_error == "not enough server space":
                    continue

            
            # Резервный вариант: file.io
            try:
                _raise_if_cancelled()
                _send_progress({
                    'phase': 'uploading',
                    'percent': 90,
                    'status': 'uploading:file.io',
                    'server': 'file.io'
                })
                with open(file_path, 'rb') as f:
                    safe_title = safe_filename(title)
                    files = {'file': (f"{safe_title}.{ext}", f)}
                    r = requests.post("https://file.io", files=files, timeout=EXTERNAL_UPLOAD_TIMEOUT_SECONDS)
                _raise_if_cancelled()
                if r.status_code < 200 or r.status_code >= 300:
                    return worker_error(
                        ERR_WORKER_UPLOAD_HTTP,
                        f"Не удалось загрузить на gofile, fallback file.io вернул HTTP {r.status_code}. Последняя ошибка gofile: {last_error}",
                    )
                try:
                    data = r.json()
                except Exception:
                    return worker_error(
                        ERR_WORKER_UPLOAD_BAD_RESPONSE,
                        f"Не удалось загрузить на gofile, fallback file.io вернул неожиданный ответ. Последняя ошибка gofile: {last_error}",
                    )
                link = data.get("link") or data.get("url")
                success = data.get("success")
                if (success is True or link) and link:
                    _send_progress({
                        'phase': 'uploaded',
                        'percent': 100,
                        'status': 'uploaded',
                        'server': 'file.io'
                    })
                    return {'status': 'ok', 'mode': 'link', 'link': link, 'title': title, 'uploader': uploader}
                err_msg = data.get("error") or data.get("message") or str(data)
                return worker_error(
                    ERR_WORKER_UPLOAD_FAILED,
                    f"Не удалось загрузить на gofile и file.io: {err_msg}. Последняя ошибка gofile: {last_error}",
                )
            except WorkerCancelledError:
                raise
            except Exception as e:
                return worker_error(
                    ERR_WORKER_UPLOAD_FAILED,
                    f"Не удалось загрузить на gofile и file.io: {e}. Последняя ошибка gofile: {last_error}",
                )
    except WorkerCancelledError as e:
        if e.reason == "stall_watchdog":
            return worker_error(
                ERR_WORKER_STALLED,
                "Загрузка зависла из-за нестабильной сети. Попробуй еще раз.",
            )
        return worker_error(
            ERR_WORKER_CANCELLED,
            "Операция отменена.",
        )
    except Exception as e:
        code = classify_exception_error_code(e)
        if code == ERR_UNKNOWN:
            code = ERR_WORKER_RUNTIME
        return worker_error(code, str(e))

# ===== Асинхронный наблюдатель прогресса (обновление раз в 6 сек) =====

async def _progress_consumer(user_id, progress_q):
    while True:
        item = await progress_q.get()
        if item is None:
            return
        entry = JOB_PROGRESS.get(user_id)
        if not entry:
            continue

        now_ts = time.time()
        entry['last_progress_ts'] = now_ts
        total = item.get('total')
        downloaded = item.get('downloaded', 0)
        old_percent = int(entry.get('percent', 0) or 0)
        old_downloaded = int(entry.get('downloaded_bytes', 0) or 0)
        progressed = False

        if downloaded > old_downloaded:
            progressed = True
            entry['downloaded_bytes'] = downloaded

        if total and total > 0:
            pct = int(downloaded * 100 / total)
            new_pct = max(0, min(100, pct))
            entry['percent'] = new_pct
            if new_pct > old_percent:
                progressed = True

        if 'percent' in item:
            forced_pct = max(0, min(100, int(item.get('percent', entry.get('percent', 0)))))
            entry['percent'] = forced_pct
            if forced_pct > old_percent:
                progressed = True

        status = item.get('status')
        info_update = {}
        if status is not None:
            info_update['status'] = status
        if 'speed' in item:
            info_update['speed'] = item.get('speed')
        if 'eta' in item:
            info_update['eta'] = item.get('eta')
        if 'server' in item:
            info_update['server'] = item.get('server')
        if 'attempt' in item:
            info_update['attempt'] = item.get('attempt')
        if info_update:
            entry['last_info'] = info_update

        if 'phase' in item:
            new_phase = item.get('phase')
            if new_phase and new_phase != entry.get('phase'):
                progressed = True
            entry['phase'] = new_phase

        if status == 'finished':
            entry['percent'] = 100
            progressed = True

        if progressed:
            entry['last_advance_ts'] = now_ts


async def _stall_watchdog(user_id, cancel_event, cancel_reason_ref, user_logs_enabled=False, job_id=None):
    if DOWNLOAD_STALL_TIMEOUT_SECONDS <= 0:
        return
    try:
        while True:
            await asyncio.sleep(DOWNLOAD_STALL_CHECK_INTERVAL_SECONDS)
            if cancel_event is not None and cancel_event.is_set():
                return

            entry = JOB_PROGRESS.get(user_id)
            if not entry:
                return
            if entry.get("done"):
                return

            phase = entry.get("phase") or "downloading"
            if phase != "downloading":
                continue

            last_advance_ts = entry.get("last_advance_ts") or entry.get("started_ts") or time.time()
            stalled_for = int(max(0, time.time() - last_advance_ts))
            if stalled_for < DOWNLOAD_STALL_TIMEOUT_SECONDS:
                continue

            request_active_download_cancel(user_id, reason="stall_watchdog")
            if cancel_reason_ref is not None and isinstance(cancel_reason_ref, list):
                if not cancel_reason_ref:
                    cancel_reason_ref.append("stall_watchdog")
                elif cancel_reason_ref[0] is None:
                    cancel_reason_ref[0] = "stall_watchdog"
            if cancel_event is not None:
                cancel_event.set()

            entry["phase"] = "stalled"
            entry["last_info"] = {"status": "stalled"}
            if user_logs_enabled:
                log_event(
                    "job.stall_watchdog_triggered",
                    level="WARNING",
                    error_code=ERR_WORKER_STALLED,
                    job_id=job_id,
                    user_id=user_id,
                    stalled_for_seconds=stalled_for,
                )
            return
    except asyncio.CancelledError:
        return
    except Exception:
        return

async def _progress_watcher(user_id, status_msg, base_text, lang):
    try:
        progress_label = t("progress_label", lang)
        done_label = t("progress_done", lang)
        while True:
            await asyncio.sleep(6)
            entry = JOB_PROGRESS.get(user_id)
            if not entry:
                return
            percent = entry.get('percent', 0)
            info = entry.get('last_info', {}) or {}
            phase = entry.get('phase', '')
            status = info.get('status', '') or ''
            eta = info.get('eta')
            base = base_text or (status_msg.text or "")
            extra = ""
            if phase == 'uploading':
                extra = t("uploading_note", lang)
            elif phase == 'uploaded':
                extra = t("uploaded_note", lang)
            elif phase == 'stalled':
                extra = t("stalled_note", lang)
            else:
                if status:
                    extra = f"{status}"
                    if eta:
                        try:
                            extra += f" | ETA: {int(eta)}s"
                        except Exception:
                            pass
            msg_text = f"{base}\n{progress_label}: {percent}%"
            if extra:
                msg_text = f"{msg_text}\n{extra}"
            try:
                await status_msg.edit_text(msg_text)
            except Exception:
                pass
            if entry.get('done'):
                try:
                    await status_msg.edit_text(f"{base}\n{progress_label}: 100% — {done_label}")
                except Exception:
                    pass
                return
    except asyncio.CancelledError:
        return
    except Exception:
        return


