"""Durable bounded parallel rendering, owned by the local server."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time

import core

STOP = threading.Event()
PARENT_PID = int(sys.argv[1]) if len(sys.argv) > 1 else None


def update(job_id, **values):
    values["updated"] = core.now()
    with core.database() as db:
        db.execute("UPDATE jobs SET " + ",".join(key + "=?" for key in values) + " WHERE id=?", (*values.values(), job_id))


def stop_requested():
    return STOP.is_set() or (PARENT_PID is not None and not core.process_alive(PARENT_PID)) or core.get_meta("worker_stop", False)


def encode(job, spec, overlay, partial, backend, log_path):
    config = spec["settings"]
    command = [core.executable("ffmpeg"), "-hide_banner", "-y", "-loglevel", "warning",
               "-filter_complex_threads", "2", "-threads", "2",
               "-ss", str(spec["start"]), "-i", spec["background"]["path"],
               "-stream_loop", "-1" if config["loop_music"] else "0", "-ss", str(spec["music_start"]),
               "-i", spec["music"]["path"], "-loop", "1", "-i", str(overlay)]
    progress_inputs = None
    metadata_input = 3
    if config.get("progress_enabled"):
        assets = core.ensure_progress_assets(spec)
        command += ["-loop", "1", "-framerate", "30", "-i", assets["base"], "-loop", "1", "-framerate", "30", "-i", assets["fill"]]
        progress_inputs, metadata_input = (3, 4), 5
    if backend == "nvidia":
        command += ["-stream_loop", "-1", "-threads", "1", "-i", str(core.hdr_metadata_reference(spec))]
    fade = min(config["fade"], spec["duration"] / 2)
    fade_filter = f",afade=t=in:st=0:d={fade},afade=t=out:st={spec['duration']-fade}:d={fade}" if fade else ""
    audio = f"[1:a:0]asetpts=PTS-STARTPTS,volume={config['music_volume']/100}{fade_filter}[music];"
    if config["original_volume"] > 0 and spec["background"]["info"]["audio"]:
        audio += f"[0:a:0]asetpts=PTS-STARTPTS,volume={config['original_volume']/100}[original];[music][original]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[a]"
    else:
        audio += "[music]alimiter=limit=0.95[a]"
    graph = core.nvenc_video_graph(spec, progress_inputs, metadata_input) if backend == "nvidia" else core.hdr_video_graph(spec, progress_inputs=progress_inputs)
    # Keep audio in its own graph: initializing an audio sink in the same graph
    # can initialize NVENC before the HDR reference's stream side data arrives.
    command += ["-filter_complex", graph, "-filter_complex", audio, "-map", "[v]", "-map", "[a]", "-t", str(spec["duration"]),
                *core.hdr_encoder_options(spec["background"]["info"], backend),
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart",
                "-progress", "pipe:1", "-nostats", str(partial)]
    with log_path.open("a", encoding="utf-8") as log:
        log.write("Command: " + subprocess.list2cmdline(command) + "\n")
        log.flush()
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log, text=True,
                                   encoding="utf-8", errors="replace", creationflags=core.CREATE_FLAGS)
        def read_progress():
            last = 0
            for line in process.stdout:
                if line.startswith("out_time_us=") and time.monotonic() - last > .6:
                    try:
                        percent = min(99, float(line.split("=", 1)[1]) / 1_000_000 / spec["duration"] * 100)
                        update(job["id"], progress=max(0, percent))
                        last = time.monotonic()
                    except (ValueError, OSError):
                        pass
        reader = threading.Thread(target=read_progress, daemon=True)
        reader.start()
        interrupted = cancelled = False
        try:
            while True:
                with core.database() as db:
                    state = db.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job["id"],)).fetchone()
                interrupted = stop_requested()
                cancelled = not state or bool(state[0])
                if interrupted or cancelled:
                    if process.poll() is None:
                        process.terminate()
                    break
                if process.poll() is not None:
                    break
                time.sleep(.25)
        finally:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=5)
            process.stdout.close()
    if interrupted or cancelled:
        raise InterruptedError("interrupted" if interrupted else "cancelled")
    if process.returncode:
        raise RuntimeError("FFmpeg завершился с кодом " + str(process.returncode))
    return core.probe(partial)


def validate_result(result, duration):
    if not result["video"] or not result["audio"] or abs(result["duration"] - duration) > .35:
        raise ValueError("Проверка готового файла не пройдена: длительность или дорожки отличаются от задания.")
    if not (result["codec"] == "hevc" and result["pix_fmt"] == "yuv420p10le" and result["color_transfer"] == "smpte2084" and result["color_primaries"] == "bt2020" and result["color_space"] == "bt2020nc"):
        raise ValueError("Проверка HDR не пройдена: файл должен быть HEVC 10 бит, PQ, BT.2020.")


def render(job):
    spec = json.loads(job["spec"])
    previous_config = spec["settings"]
    config = {**core.DEFAULTS, **previous_config, "export_profile": "hdr10"}
    if "text_x" not in previous_config:
        config["text_x"] = 46
    spec["settings"] = config
    output = Path(job["output"])
    partial = output.with_name(output.stem + ".part.mp4")
    overlay = Path(spec["overlay"])
    started = time.monotonic()
    try:
        runtime = core.settings()
        config.update(encoder_mode=runtime["encoder_mode"], parallel_jobs=runtime["parallel_jobs"])
        hardware = core.get_meta("hardware", {})
        backend = "nvidia" if config["encoder_mode"] != "cpu" and hardware.get("nvenc") else "cpu"
        spec["rendering"] = {"requested": config["encoder_mode"], "encoder": backend, "phase": "encoding"}
        if config["encoder_mode"] != "cpu" and backend == "cpu":
            spec["rendering"]["fallback"] = hardware.get("reason") or "NVIDIA недоступна: используется CPU."
        spec["background"]["info"] = core.probe(spec["background"]["path"])
        update(job["id"], spec=json.dumps(spec, ensure_ascii=False))
        if shutil_free(output.parent) < 200_000_000 + int(spec["duration"] * 900_000):
            raise ValueError("Недостаточно свободного места на диске.")
        if not overlay.is_file():
            core.make_overlay(spec["text"], config, overlay)
        core.ensure_progress_assets(spec)
        update(job["id"], spec=json.dumps(spec, ensure_ascii=False))
        log_path = core.ROOT / "logs" / (job["id"] + ".log")
        log_path.write_text("", encoding="utf-8")
        try:
            result = encode(job, spec, overlay, partial, backend, log_path)
            validate_result(result, spec["duration"])
        except InterruptedError:
            raise
        except Exception:
            if backend != "nvidia":
                raise
            logging.exception("NVENC failed for %s; retrying the same job on CPU", job["id"])
            partial.unlink(missing_ok=True)
            spec["rendering"].update(encoder="cpu", fallback="Сбой NVIDIA: используется CPU. Подробности в logs.")
            update(job["id"], progress=0, spec=json.dumps(spec, ensure_ascii=False))
            with log_path.open("a", encoding="utf-8") as log:
                log.write("\nNVENC failed. Retrying on CPU.\n")
            result = encode(job, spec, overlay, partial, "cpu", log_path)
            validate_result(result, spec["duration"])
        spec["rendering"]["phase"] = "preview"
        update(job["id"], spec=json.dumps(spec, ensure_ascii=False))
        watch_path = core.ROOT / "output/previews" / (job["id"] + "_watch.mp4")
        def must_cancel():
            with core.database() as db:
                flag = db.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job["id"],)).fetchone()
            return stop_requested() or bool(flag[0])
        try:
            core.render_watch_copy(partial, watch_path, must_cancel)
        except InterruptedError:
            pass
        except Exception:
            logging.exception("Browser viewing copy failed for %s", job["id"])
        if must_cancel():
            partial.unlink(missing_ok=True)
            watch_path.unlink(missing_ok=True)
            update(job["id"], status="interrupted" if stop_requested() else "cancelled", progress=0, error="")
            return
        os.replace(partial, output)
        output.with_suffix(".txt").write_text(spec["caption"], encoding="utf-8")
        spec["rendering"].update(phase="done", elapsed_seconds=round(time.monotonic() - started, 1))
        update(job["id"], status="done", progress=100, error="", spec=json.dumps(spec, ensure_ascii=False))
    except InterruptedError as exc:
        partial.unlink(missing_ok=True)
        interrupted = str(exc) == "interrupted"
        update(job["id"], status="interrupted" if interrupted else "cancelled", progress=0,
               error="Сборка прервана при закрытии программы." if interrupted else "")
    except Exception as exc:
        partial.unlink(missing_ok=True)
        logging.exception("Job %s failed", job["id"])
        update(job["id"], status="error", error=str(exc))
    finally:
        try:
            core.write_manifest(job["batch_id"])
        except Exception:
            logging.exception("Could not update manifest for %s", job["batch_id"])


def shutil_free(folder):
    import shutil
    folder.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(folder).free


def main():
    core.initialize()
    logging.basicConfig(filename=core.ROOT / "logs/worker.log", encoding="utf-8", level=logging.INFO)
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: STOP.set())
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="render") as executor:
        active = set()
        while not stop_requested():
            finished = {future for future in active if future.done()}
            for future in finished:
                try:
                    future.result()
                except Exception:
                    logging.exception("Unexpected render task failure")
            active -= finished
            limit = max(1, min(3, int(core.settings().get("parallel_jobs", 2))))
            if len(active) >= limit:
                STOP.wait(.25)
                continue
            with core.database() as db:
                db.execute("BEGIN IMMEDIATE")
                paused = db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()
                stopped = db.execute("SELECT value FROM meta WHERE key='worker_stop'").fetchone()
                row = None
                if not (paused and json.loads(paused[0])) and not (stopped and json.loads(stopped[0])):
                    row = db.execute("SELECT * FROM jobs WHERE status='queued' AND cancel_requested=0 ORDER BY created,ordinal LIMIT 1").fetchone()
                    if row:
                        db.execute("UPDATE jobs SET status='rendering',progress=0,error='',updated=? WHERE id=? AND status='queued'", (core.now(), row["id"]))
            if row:
                active.add(executor.submit(render, dict(row)))
            else:
                STOP.wait(.35)



if __name__ == "__main__":
    main()
