#!/usr/bin/env python3
"""Linux A/B comparison of prebuilt servers. See ../README.md for the quick loop."""
import argparse
import base64
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import platform
import resource
import shutil
import socket
import statistics
import subprocess
import tempfile
import time
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SECRET = "twelve-dollar-challenge"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cpu_seconds(pid):
    # /proc/PID/stat aggregates the event loop and checkpoint thread.
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def core(cpu):
    base = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
    return tuple((base / name).read_text().strip()
                 for name in ("physical_package_id", "core_id"))


def tokens(path):
    def b64(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()
    head = b64(b'{"alg":"HS256","typ":"JWT"}')
    users = []
    # Fresh tokens: seed/tokens.json may have expired since seed generation.
    for uid in range(1, 2001):
        payload = b64(json.dumps({"sub": str(uid), "username": f"bench_{uid}",
                                 "exp": int(time.time()) + 86400}).encode())
        message = f"{head}.{payload}"
        sig = b64(hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).digest())
        users.append({"token": f"{message}.{sig}"})
    path.write_text(json.dumps(users))


@contextlib.contextmanager
def server(binary, seed, cpu, directory):
    db = directory / "feed.db"
    shutil.copyfile(seed, db)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GOOSE_")}
    env.update(SQLITE_PATH=str(db), JWT_SECRET=SECRET, HOST="127.0.0.1", PORT=str(port))

    def limits():
        os.sched_setaffinity(0, {cpu})
        _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        cap = min(65535, hard) if hard != resource.RLIM_INFINITY else 65535
        resource.setrlimit(resource.RLIMIT_NOFILE, (cap, cap))

    with (directory / "server.log").open("w+") as log:
        p = subprocess.Popen([str(binary)], env=env, stdout=log, stderr=log, preexec_fn=limits)
        try:
            url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 15
            while True:
                if p.poll() is not None or time.monotonic() > deadline:
                    log.seek(0)
                    raise RuntimeError("server did not start:\n" + log.read())
                try:
                    with urllib.request.urlopen(url + "/health", timeout=0.5) as r:
                        if r.status == 200:
                            break
                except OSError:
                    time.sleep(0.05)
            yield p, port, url
        finally:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()


def wire_checks(port):
    # A fragmented head, then pipelined requests; compare bodies independently to goldens.
    paths = ["/feed?split=yes", "/posts/1", "/posts/250000"]
    expected = ["feed.json", "post-1.json", "post-250000.json"]
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        first = f"GET {paths[0]} HTTP/1.1\r\nHost: bench\r\n\r\n".encode()
        s.sendall(first[:7])
        time.sleep(0.01)
        s.sendall(first[7:] + b"".join(
            f"GET {path} HTTP/1.1\r\nHost: bench\r\n\r\n".encode() for path in paths[1:]))
        with s.makefile("rb") as response:
            for name in expected:
                assert response.readline().split()[1] == b"200", "wire status"
                headers = {}
                while (line := response.readline()) != b"\r\n":
                    assert line, "truncated response headers"
                    key, value = line.split(b":", 1)
                    headers[key.lower()] = value.strip()
                body = response.read(int(headers[b"content-length"]))
                assert body.rstrip(b"\n") == (ROOT / "test/golden" / name).read_bytes().rstrip(b"\n"), name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--wrk", type=Path, default=HERE.parent / ".build/wrk/wrk")
    parser.add_argument("--seed", type=Path, default=ROOT / "seed/feed.db")
    parser.add_argument("--seconds", type=int, default=5)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--connections", type=int, default=256)
    parser.add_argument("--workloads", nargs="+", choices=["feed", "post", "mixed"], default=["feed", "post", "mixed"])
    parser.add_argument("--server-cpu", type=int)
    parser.add_argument("--client-cpus", help="comma-separated CPUs on different physical cores from the server")
    parser.add_argument("--output", type=Path, required=True)
    a = parser.parse_args()
    if min(a.seconds, a.rounds, a.connections) < 1:
        parser.error("seconds, rounds and connections must be positive")
    allowed = sorted(os.sched_getaffinity(0))
    server_cpu = a.server_cpu if a.server_cpu is not None else allowed[0]
    clients = ([int(x) for x in a.client_cpus.split(",")] if a.client_cpus else
               [c for c in allowed if core(c) != core(server_cpu)][:4])
    if (server_cpu not in allowed or not clients or not set(clients) <= set(allowed)
            or any(core(c) == core(server_cpu) for c in clients)):
        parser.error("server and client CPUs must be allowed and on separate physical cores")
    if a.connections < len(clients):
        parser.error("connections must be at least the number of client threads")
    for path in (a.baseline, a.candidate, a.wrk, a.seed):
        if not path.is_file():
            parser.error(f"missing {path}; see README for setup")
    variants = {"baseline": a.baseline.resolve(), "candidate": a.candidate.resolve()}
    a.wrk = a.wrk.resolve()
    a.output.parent.mkdir(parents=True, exist_ok=True)
    (HERE.parent / ".build").mkdir(exist_ok=True)
    lock = (HERE.parent / ".build/compare.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    report = {"metadata": {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "platform": platform.platform(), "server_cpu": server_cpu, "client_cpus": clients,
              "seconds": a.seconds, "rounds": a.rounds, "connections": a.connections,
              "seed_sha256": sha(a.seed), "wrk_sha256": sha(a.wrk), "load_sha256": sha(HERE / "load.lua"),
              "binaries": {k: {"path": str(v), "sha256": sha(v)} for k, v in variants.items()}},
              "validation": {}, "runs": []}

    def save():
        a.output.write_text(json.dumps(report, indent=2) + "\n")

    with tempfile.TemporaryDirectory(prefix="goose-compare-") as tmp:
        tmp = Path(tmp)
        token_file = tmp / "tokens.json"
        tokens(token_file)
        try:
            for name, binary in variants.items():
                with tempfile.TemporaryDirectory(dir=tmp) as run:
                    with server(binary, a.seed, server_cpu, Path(run)) as (_, port, url):
                        wire_checks(port)
                        check = subprocess.run(["bash", str(ROOT / "test/test.sh"), url],
                                               env=dict(os.environ, JWT_SECRET=SECRET),
                                               capture_output=True, text=True, timeout=120)
                        report["validation"][name] = {"exit_code": check.returncode,
                                                      "wire_checks": "passed", "output": check.stdout + check.stderr}
                        save()
                        if check.returncode:
                            raise RuntimeError(f"{name} correctness failed:\n{check.stdout}\n{check.stderr}")
                        print(f"{name}: API and fragmented/pipelined checks passed", flush=True)
            for trial in range(a.rounds):
                for wi, workload in enumerate(a.workloads):
                    order = list(variants) if (trial + wi) % 2 == 0 else list(reversed(variants))
                    for name in order:
                        with tempfile.TemporaryDirectory(dir=tmp) as run:
                            with server(variants[name], a.seed, server_cpu, Path(run)) as (p, _, url):
                                def load(seconds):
                                    cmd = ["taskset", "-c", ",".join(map(str, clients)), str(a.wrk),
                                           f"-t{len(clients)}", f"-c{a.connections}", f"-d{seconds}s", "--latency",
                                           "-s", str(HERE / "load.lua"), url]
                                    result = subprocess.run(cmd, env=dict(os.environ, TOKENS=str(token_file), WORKLOAD=workload),
                                                            capture_output=True, text=True, timeout=seconds + 20)
                                    raw = result.stdout + result.stderr
                                    if result.returncode or "RESULT " not in raw:
                                        raise RuntimeError(raw)
                                    row = json.loads(raw.split("RESULT ", 1)[1].splitlines()[0])
                                    if row["errors"] or row["semantic_errors"] or not row["requests"]:
                                        raise RuntimeError("load failed:\n" + raw)
                                    return row, raw
                                load(1)
                                before = cpu_seconds(p.pid)
                                row, raw = load(a.seconds)
                                cpu = cpu_seconds(p.pid) - before
                                row.update(variant=name, workload=workload, trial=trial, raw=raw,
                                           rps=row["requests"] / row["seconds"], cpu_seconds=cpu,
                                           cpu_us_per_request=cpu * 1e6 / row["requests"],
                                           server_cpu_fraction=cpu / row["seconds"])
                                report["runs"].append(row)
                                save()
                                print(f"{trial + 1} {name:9} {workload:5}: {row['rps']:9.0f} req/s, "
                                      f"{row['cpu_us_per_request']:.2f} CPU µs/req, "
                                      f"{row['server_cpu_fraction']:.0%} CPU, p99 {row['p99_us']/1000:.2f} ms", flush=True)
            print("\nMedian candidate change (negative CPU cost is better):")
            for workload in a.workloads:
                values = {name: [r for r in report["runs"] if r["variant"] == name and r["workload"] == workload]
                          for name in variants}
                changes = {}
                for metric in ("rps", "cpu_us_per_request"):
                    b, c = (statistics.median(r[metric] for r in values[name]) for name in variants)
                    changes[metric] = 100 * (c / b - 1)
                print(f"{workload:5}: req/s {changes['rps']:+.1f}%, CPU µs/req {changes['cpu_us_per_request']:+.1f}%")
                paired = [100 * (c["cpu_us_per_request"] / b["cpu_us_per_request"] - 1)
                          for b, c in zip(values["baseline"], values["candidate"])]
                print(f"       paired CPU changes: {', '.join(f'{x:+.1f}%' for x in paired)}")
            if any(r["server_cpu_fraction"] < .85 for r in report["runs"]):
                print("WARNING: server below 85% CPU in some runs; throughput may be client/latency limited.")
        except Exception as error:
            report["error"] = str(error)
            raise
        finally:
            save()


if __name__ == "__main__":
    main()
