import argparse
import json
import math
import shlex
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid


def operator_config(path, online):
    config = json.loads(Path(path).read_text()) if path else {}
    if not isinstance(config, dict):
        raise ValueError("Operator configuration must be a JSON object")
    if online:
        config.setdefault("cache_root", str(Path.home() / ".cache" / "dopplergeist"))
        config["offline"] = False
    return config


def positive_runtime(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Runtime must be positive and finite")
    return number


def positive_int(value):
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Value must be a positive integer") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("Value must be a positive integer")
    return number


def listen_port(value):
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Port must be between 0 and 65535") from error
    if not 0 <= number <= 65535:
        raise argparse.ArgumentTypeError("Port must be between 0 and 65535")
    return number


def job_identifier(value):
    try:
        if uuid.UUID(value).hex != value:
            raise ValueError
    except ValueError as error:
        raise argparse.ArgumentTypeError("job_id must be the 32-character job identifier") from error
    return value


def post_job_action(server_url, job_id, action, parser):
    url=server_url.rstrip("/")+f"/api/jobs/{job_id}/{action}"
    request=Request(url,data=b"",method="POST")
    try:
        with urlopen(request,timeout=10) as response:
            body=response.read()
    except HTTPError as exc:
        try:
            detail=json.loads(exc.read()).get("error",str(exc))
        except (AttributeError,ValueError):
            detail=str(exc)
        parser.error(f"Could not {action} job {job_id}: HTTP {exc.code}: {detail}")
    except (URLError,ValueError) as exc:
        parser.error(f"Could not reach job service at {server_url}: {exc}")
    try:
        return json.loads(body)
    except ValueError as exc:
        parser.error(f"Job service returned invalid JSON when requesting {action} for {job_id}: {exc}")


def print_terminal_result(result):
    print(json.dumps(result, indent=2))
    if result.get("status") == "failed":
        raise SystemExit(1)
    if result.get("status") == "cancelled":
        raise SystemExit(130)


def main():
    parser=argparse.ArgumentParser(description="Aircraft acoustic geolocation research pipeline")
    try:
        installed_version=version("dopplergeist")
    except PackageNotFoundError:
        installed_version="development"
    parser.add_argument("--version",action="version",version=f"%(prog)s {installed_version}")
    sub=parser.add_subparsers(dest="command",required=True)
    run=sub.add_parser("run",help="Measure events, filter geometry, rank and refine locations")
    run.add_argument("config")
    run.add_argument("--output",required=True)
    measure=sub.add_parser("measure",help="Measure one Doppler event without locating it")
    measure.add_argument("audio")
    measure.add_argument("--start",type=float,required=True)
    measure.add_argument("--end",type=float,required=True)
    measure.add_argument("--band",type=float,nargs=2,required=True)
    measure.add_argument("--output",required=True)
    discover=sub.add_parser("discover",help="Scan a complete recording for acoustic event proposals")
    discover.add_argument("audio")
    discover.add_argument("--output",required=True,help="New directory for the scan and diagnostics")
    discover.add_argument("--plot-summary",action="store_true",help="Render a recording-level ridge overview")
    discover.add_argument("--plot-event-details",action="store_true",help="Render a spectrogram for each proposal")
    analyze=sub.add_parser("analyze",help="Automatically locate aircraft audio, or explain insufficient evidence")
    analyze.add_argument("audio")
    analyze.add_argument("--start-utc",help="Earliest UTC recording start, including explicit timezone")
    analyze.add_argument("--end-utc",help="Latest UTC recording start; defaults to earliest")
    analyze.add_argument("--time-source",help="How the recording start window was established")
    analyze.add_argument("--country",type=str.upper,choices=("US", "DE", "ES", "FR", "IT"),
                         help="Receiver country: US (CONUS, default), DE (Germany), ES (Spain), FR (metropolitan France, including Corsica), or IT (Italy, including Sicily and Sardinia)")
    analyze.add_argument("--bounds",type=float,nargs=4,metavar=("WEST", "SOUTH", "EAST", "NORTH"),
                         help="Limit the selected country profile to a WGS84 rectangle in decimal degrees")
    analyze.add_argument("--jobs",default="jobs",help="Persistent job directory")
    analyze.add_argument("--config",help="Operator provider/cache/search configuration JSON")
    analyze.add_argument("--online",action="store_true",help="Download public data to the local cache; this can require tens of gigabytes and hours")
    analyze.add_argument("--max-runtime",type=positive_runtime,default=3600,help="Wall-time limit in seconds")
    analyze.add_argument("--max-upload-mb",type=positive_int,default=100,help="Maximum recording size in MiB (default: 100)")
    serve=sub.add_parser("serve",help="Launch the local MP3 upload interface and job API")
    serve.add_argument("--jobs",default="jobs")
    serve.add_argument("--config",help="Operator provider/cache/search configuration JSON")
    serve.add_argument("--online",action="store_true",help="Download public data to the local cache; this can require tens of gigabytes and hours")
    serve.add_argument("--host",choices=("127.0.0.1", "localhost", "0.0.0.0"),default="127.0.0.1",
                       help="Bind address; use 0.0.0.0 inside a container with a loopback-published port")
    serve.add_argument("--port",type=listen_port,default=8765)
    serve.add_argument("--max-runtime",type=positive_runtime,default=3600)
    serve.add_argument("--max-upload-mb",type=positive_int,default=100)
    job=sub.add_parser("job",help="Read a persisted job result without rerunning it")
    job.add_argument("job_id",type=job_identifier)
    job.add_argument("--jobs",default="jobs")
    job.add_argument("--watch",action="store_true",help="Watch local checkpoints until the job stops")
    job.add_argument("--interval",type=positive_runtime,default=10,help="Watch interval in seconds")
    cancel=sub.add_parser("cancel",help="Request cancellation of a managed job")
    cancel.add_argument("job_id",type=job_identifier)
    cancel_target=cancel.add_mutually_exclusive_group()
    cancel_target.add_argument("--server-url",default="http://127.0.0.1:8765",
                               help="Base URL of the service managing this job")
    cancel_target.add_argument("--jobs",help="Persistent job directory for cooperative cancellation")
    resume=sub.add_parser("resume",help="Resume a stopped job after completed audio discovery")
    resume.add_argument("job_id",type=job_identifier)
    resume.add_argument("--jobs",default="jobs")
    resume.add_argument("--server-url",help="Base URL of the service managing this job")
    resume.add_argument("--max-runtime",type=positive_runtime,default=3600)
    args=parser.parse_args()
    if args.command in {"analyze", "serve"}:
        try:
            config=operator_config(args.config,args.online)
        except (OSError, ValueError) as exc:
            parser.error(f"Invalid operator configuration: {exc}")
    if args.command=="run":
        from .pipeline import run
        run(args.config,args.output)
    elif args.command=="measure":
        from .audio import measure
        result=measure(args.audio,{"start_s":args.start,"end_s":args.end,"band_hz":args.band},args.output)
        print(json.dumps(result[-1],indent=2))
    elif args.command=="discover":
        from .intake import intake_recording
        from .discovery import discover_events
        output=Path(args.output)
        try:
            output.mkdir(parents=True,exist_ok=False)
            recording=intake_recording(args.audio,output/"intake")
            events=discover_events(recording,output_dir=output/"discovery",
                                   plot_summary=args.plot_summary,plot_event_details=args.plot_event_details)
            summary={"recording":recording.metadata,"time_status":recording.time_status,
                     "events_found":len(events),
                     "groups_found":len({event.get("harmonic_group_id") or event["event_id"] for event in events}),
                     "scope":"Acoustic proposals only; aircraft identity and location are not established"}
            (output/"summary.json").write_text(json.dumps(summary,indent=2,allow_nan=False)+"\n")
        except (OSError, ValueError) as exc:
            parser.error(f"Could not complete discovery: {exc}")
        print(json.dumps(summary,indent=2))
    elif args.command=="serve":
        from .service import serve
        try:
            serve(args.jobs,config,host=args.host,port=args.port,max_runtime_s=args.max_runtime,
                  max_upload_bytes=args.max_upload_mb*1024*1024)
        except (OSError, ValueError, OverflowError) as exc:
            parser.error(f"Could not start local service: {exc}")
    elif args.command=="analyze":
        from .jobs import JobManager
        from .contracts import validate_request
        from .geography import profile_for_search
        audio=Path(args.audio)
        if not audio.is_file():
            parser.error(f"Audio file is unavailable or not a regular file: {audio}")
        try:
            audio_size=audio.stat().st_size
        except OSError as exc:
            parser.error(f"Could not read recording: {exc}")
        if audio_size > args.max_upload_mb*1024*1024:
            parser.error(f"Recording exceeds the {args.max_upload_mb} MiB limit; increase --max-upload-mb")
        if args.time_source and not args.start_utc:
            parser.error("--time-source requires --start-utc")
        if args.end_utc and not args.start_utc:
            parser.error("--end-utc requires --start-utc")
        request={"schema_version":1,"search":{"coverage":"conus"}}
        if args.country is not None:
            profile=profile_for_search({"country":args.country})
            request["search"]={"country":profile.country,"coverage":profile.coverage}
        if args.bounds is not None:
            request["search"]["bounds_wsen"]=args.bounds
        if args.start_utc:
            request["time_window"]={"start_utc":args.start_utc,"end_utc":args.end_utc or args.start_utc,"source":"user"}
            if args.time_source:
                request["time_window"]["source_detail"]=args.time_source
        try:
            validate_request(dict(request, audio_path=str(audio)))
        except ValueError as exc:
            parser.error(str(exc))
        try:
            manager=JobManager(args.jobs,config,max_runtime_s=args.max_runtime,
                               max_upload_bytes=args.max_upload_mb*1024*1024)
        except (OSError, ValueError, OverflowError) as exc:
            parser.error(f"Could not open job store: {exc}")
        try:
            try:
                with audio.open("rb") as source:
                    queued=manager.submit(request,source,audio_size,suffix=audio.suffix)
            except (OSError, ValueError, OverflowError) as exc:
                parser.error(f"Could not submit recording: {exc}")
            print(f"Job {queued['job_id']} started. Monitor: dopplergeist job {queued['job_id']} "
                  f"--jobs {shlex.quote(str(args.jobs))} --watch",file=sys.stderr,flush=True)
            print_terminal_result(manager.wait(queued["job_id"]))
        finally:
            manager.close()
    elif args.command=="job":
        from .jobs import read_json, status_for_display
        directory=Path(args.jobs)/args.job_id
        previous_count=previous_time=previous_fine_support_count=previous_split_count=None
        stale_after=60.0
        try:
            while True:
                try:
                    value = status_for_display(directory, read_json(directory/"result.json"),
                                               stale_after_s=stale_after)
                except (OSError, ValueError) as exc:
                    parser.error(f"Could not read job {args.job_id}: {exc}")
                if not args.watch:
                    break
                search=directory/"search"
                tiles=[path for path in search.glob("tile-*.json") if not path.name.endswith(".error.json")]
                fine_files=[path for path in search.glob("fine-tile-*.json")
                            if not path.name.endswith(".error.json")]
                split_count=sum(1 for _ in search.glob("split-*.json"))
                support_files=list((search/"support").glob("*.json"))
                fine_support_files=[path for path in support_files if path.name.startswith("fine-")]
                fine_support_count=len(fine_support_files)
                count=len(tiles)+len(fine_files)
                wall_now=time.time()
                progress=value.get("progress")
                result_age=max(0.0,wall_now-(directory/"result.json").stat().st_mtime)
                age_text=lambda seconds: f"{seconds:.0f}s" if seconds<60 else f"{seconds/60:.1f}m"
                progress_stale=value.get("progress_stale", False)
                if progress_stale:
                    progress_text=f", overall progress hidden (last update {age_text(result_age)} ago)"
                else:
                    progress_text=(f", overall progress {progress:.1%}"
                                   if isinstance(progress,(int,float)) and math.isfinite(progress) else "")
                stage_message=value.get("message")
                activity_text=(f", {stage_message}"
                               if value.get("stage") in {"acquisition", "search", "refinement", "decision"}
                               and isinstance(stage_message,str) else "")
                now=time.monotonic()
                rate=""
                if previous_count is not None and count >= previous_count and now > previous_time:
                    rate=f", file-count rate {(count-previous_count)*60/(now-previous_time):.1f} files/min"
                split_rate=""
                if previous_split_count is not None and split_count >= previous_split_count and now > previous_time:
                    split_rate=(f", coarse split-marker rate "
                                f"{(split_count-previous_split_count)*60/(now-previous_time):.1f} markers/min")
                fine_support_rate=""
                if (previous_fine_support_count is not None
                        and fine_support_count >= previous_fine_support_count and now > previous_time):
                    fine_support_rate=(f", fine support record rate "
                                       f"{(fine_support_count-previous_fine_support_count)*60/(now-previous_time):.1f} records/min")
                search_files=tiles+fine_files
                # Support diagnostics, worker ledgers and retention writes show
                # activity, not completed payloads. Keep them outside the count and rate.
                activity_files=(search_files+support_files
                                +list((search/".tile-workers").glob("*.sqlite*"))
                                +[search/"retention.sqlite", search/"retention.sqlite-wal"])
                modified={}
                for path in activity_files:
                    try:
                        modified[path]=path.stat().st_mtime
                    except FileNotFoundError:
                        # A WAL can disappear during a normal SQLite checkpoint.
                        continue
                latest=max((modified[path] for path in search_files if path in modified),default=None)
                file_age=(f", newest matching file modified {age_text(max(0.0,wall_now-latest))} ago"
                          if latest is not None else "")
                latest_fine_support=max((modified[path] for path in fine_support_files if path in modified),
                                        default=None)
                fine_support_age=(f", newest fine support record modified "
                                  f"{age_text(max(0.0,wall_now-latest_fine_support))} ago"
                                  if latest_fine_support is not None else "")
                activity=max(modified,key=modified.get,default=None)
                activity_age=(f", newest search write {age_text(max(0.0,wall_now-modified[activity]))} ago "
                              f"({activity.relative_to(search)}; unverified file activity)"
                              if activity is not None else "")
                print(f"{value['status']} ({value.get('stage', 'no stage')}{progress_text}{activity_text}): "
                      f"{count} search files observed (unverified: {len(tiles)} coarse, "
                      f"{len(fine_files)} fine, {split_count} coarse split markers){rate}{split_rate}{file_age}, "
                      f"{fine_support_count} fine support records observed (unverified)"
                      f"{fine_support_rate}{fine_support_age}{activity_age}, "
                      f"status updated {age_text(result_age)} ago",file=sys.stderr,flush=True)
                if value["status"] in {"needs_input", "succeeded", "inconclusive", "failed", "cancelled"}:
                    break
                previous_count,previous_time,previous_fine_support_count,previous_split_count=(
                    count,now,fine_support_count,split_count)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("Stopped watching; the job continues.",file=sys.stderr)
            raise SystemExit(130)
        print(json.dumps(value,indent=2))
    elif args.command=="cancel":
        if args.jobs is not None:
            from .jobs import request_local_cancel
            try:
                value=request_local_cancel(args.jobs,args.job_id)
            except (KeyError,OSError,ValueError) as exc:
                parser.error(f"Could not request cancellation for job {args.job_id}: {exc}")
        else:
            value=post_job_action(args.server_url,args.job_id,"cancel",parser)
        print(json.dumps(value,indent=2))
    elif args.command=="resume":
        if args.server_url:
            value=post_job_action(args.server_url,args.job_id,"resume",parser)
            print(json.dumps(value,indent=2))
            return
        from .jobs import JobManager
        try:
            manager=JobManager(args.jobs,max_runtime_s=args.max_runtime)
        except (OSError, ValueError) as exc:
            parser.error(f"Could not open job store: {exc}")
        try:
            manager.resume(args.job_id)
            print_terminal_result(manager.wait(args.job_id))
        except (KeyError, OSError, OverflowError, ValueError) as exc:
            parser.error(f"Could not resume job {args.job_id}: {exc}")
        finally:
            manager.close()


if __name__=="__main__":
    main()
