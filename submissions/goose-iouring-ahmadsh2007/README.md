# Goose: an io_uring server written in Goose, SQLite B-tree reads

One thread serves every connection through io_uring and every request through one SQLite
transaction per pass. The server is [Goose](https://github.com/aardappel/goose), a memory-safe
systems language with no heap, no GC and no lifetime annotations, which compiles to C: the
event loop, the connection table, the accept and ring policy, batching and group commit, write
coalescing, the checkpoint policy, HTTP, JSON, HS256 and the read plans are all `app.goose`. C
is only what Goose cannot do: talk to the kernel (`uring.h`) and be SQLite (`sqlite.c`).

| | |
|---|---|
| Language | Goose, compiler pinned at commit `74c641c5` (runtime `3025e5810379`), built from source by `install.sh` (sha256-checked) |
| Framework | none: the event loop, HTTP/1.1, JSON and HS256 (SHA-256 and HMAC included) are written in Goose here |
| io_uring | liburing 2.15, pinned tarball (sha256), static |
| SQLite | 3.54.1 amalgamation (sha256), unmodified, compiled into `sqlite.c` |
| C compiler | gcc 13 (Ubuntu 24.04 `build-essential`), `-O3`, profile-guided, static |
| **Nginx or direct** | **Direct**: serves `HOST:PORT` itself (`0.0.0.0:80` on the benchmark box) |
| Needs | Linux 6.1+ (io_uring with `DEFER_TASKRUN`, multishot receive, provided-buffer rings, direct descriptors); tested on Ubuntu 24.04's 6.8 |

## History on the box

| Commit | Users held | What changed |
|---|---:|---|
| `204b6b0` | 260,000 | first version; its C was a port of rust-iouring-kensac's transport and helpers |
| `67ef830` | 270,000 | the event loop moved into Goose (new `uring.c`, `sqlite.c`), coalesced writes, a new checkpoint policy |
| this one | (not run yet) | receive buffers that a burst cannot run dry, one write per commit, longer checkpoint cycles |

Both scored versions failed just above their score the same way: p99 in seconds with 0 errors
and CPU to spare (`67ef830`: 280,000 at p99 1.9 s, 78 % CPU). The same signature reproduced
locally (one dedicated vCPU, the user model at 2-3x its rate), and per-second counters in the
loop showed what it is:

- **A burst runs a ring out of receive buffers, and the server never catches up.** A pass that
  runs long lets more messages pile up for the next one than a ring has buffers (4,096 then);
  the kernel ends each starved connection's multishot receive with `ENOBUFS`, every one must
  be armed again (58,746 in the worst second of a failing run), which makes the next pass
  longer still. Each ring now has 16,384 buffers of 1 KB (a request is a few hundred bytes; a
  longer one spans buffers), the same memory per buffer in use. The failing run (300,000 users
  at 2.8x) went from p95 573 ms to 42 ms, with no exhausted buffer at all.
- For `204b6b0` the evidence pointed at the log: per-pass commits wrote many small sets of
  frames, the checkpointer fell behind (the WAL reached 143 MB against kensac's 24 MB), and
  holds and the loop's own last-resort checkpoint followed. `67ef830` fixed that (coalesced
  writes, a checkpoint policy that never runs on the loop).
- **One write per commit.** SQLite appends each log frame with two system calls, so a commit
  of twenty frames was forty. A thin VFS over unix-excl now gathers a commit's frames and
  writes them at once (below): 82 % fewer `pwrite` calls, about 2 % less CPU per request
  locally, likely more on the box, where a system call costs more.
- Longer checkpoint cycles (16,384 frames): fewer holds and fewer log restarts (each restart
  syncs the log's header on the loop, which a VPS disk can make slow), about 3 % less CPU.
- Spare response buffers are capped, so a burst cannot pin memory for good.

## What is Goose and what is C

| `app.goose` (Goose) | `uring.h`, `sqlite.c` (C) |
|---|---|
| the loop: waiting, draining every ring's completions, flushing after COMMIT, re-arming receives | `io_uring_enter`, queueing operations, copying completions into a Goose array |
| the connection table: generations, flags, which box holds what, partial and held requests | numbered byte boxes (C memory, since the kernel reads them asynchronously) |
| accepting: how many in flight, which ring, when a ring is full, when to open another | rings, sparse direct-descriptor tables, provided buffers, listening sockets |
| write coalescing, the checkpoint policy, when writes wait | the checkpoint thread (it copies when asked) |
| HTTP/1.1 framing and parsing, routing, every status code and error message | |
| HS256: base64url, SHA-256, HMAC (precomputed pad states), the claims | |
| a validating JSON reader (any document, escapes, surrogate pairs, UTF-8) and writer (8-byte SWAR escaping) | |
| the body's JavaScript `trim()` and code-point count | |
| SQLite's record format: varints, serial types, column ranges | moving SQLite's B-tree cursors, copying a row's record bytes |
| the feed and post read plans and their fallback to SQL | BEGIN/COMMIT, the two INSERTs, `SELECT 1` |

This server's boundary uses copies, and no Goose memory is shared with the kernel: C appends
a message's bytes and a row's record bytes to Goose builders, and Goose's finished responses
are copied into a box for sending. `app.build` includes `uring.h` through `c_headers`, using
Goose's generated slice and completion types. The C compiler can inline the transport into
the event loop; buffer ownership, queue policy and SQLite compilation are unchanged.

## A pass

1. Every other ring submits and collects without waiting; ring 0 goes last and waits for 256
   completions or 1 ms, so a busy pass shares one COMMIT across many requests.
2. Each ring's completions are copied into a Goose array and handled in order. A receive is
   served at once: Goose parses every complete request in the message (after any bytes kept
   from an earlier one), answers it into the connection's output box, and keeps the rest.
3. Writes that are due run (below). Every request that touches the database joins the pass's
   transaction, begun by the first one.
4. `COMMIT`, then every touched connection's output goes to the kernel: **no response from a
   pass is sent before its COMMIT returns** (group commit, rule 6). If the commit fails, every
   answer of the pass becomes a 500 and its connection closes, so a 201 never claims a lost
   write.
5. Receives that ended are re-armed, accepts topped up, and the checkpoint policy runs.

## Connections

- Accepted sockets are io_uring **direct descriptors** in a ring's file table, so
  `LimitNOFILE=65535` caps a ring, not the server. A ring takes accepts until fewer than
  (accepts in flight + 64) slots are free, then the next ring with room takes over (one is
  added when none has room, up to 16 rings: over a million connections). Accepts are
  single-shot, 512 in flight, spread over eight `SO_REUSEPORT` listeners (eight accept queues).
- One **multishot receive** per connection into the ring's provided buffers (16,384 x 1 KB):
  an idle connection holds no buffer, only its 24-byte record in Goose's table.
- One send in flight per connection; answers to pipelined requests queue behind it, in order.
  Closing is a `SHUTDOWN` hard-linked to a direct `CLOSE`; a generation number in every
  `user_data` drops completions for a reused slot. Idle connections are never closed.
- Ubuntu's 6.8.0-136+ kernels refuse a buffer-ring registration with zero reserved words
  (Launchpad #2162843); a refused registration is retried once with `resv[0] = 1`.
- The binary's code (static, ~2 MB) is `mlock`ed at startup: with no swap, reclaim near the
  memory wall takes file-backed pages first, and the loop must not fault its own code back in.

## Writes: coalesced, then group-committed

A write (`POST`) does not run in the pass it arrives in. It waits, unread, with its connection
(and so does anything pipelined behind it) until a window that opened with the first waiting
write ends, 2 ms later; then all waiting writes run in one pass and share its COMMIT. At the
load test's rate that is one commit with writes every couple of milliseconds instead of one per
pass, and the pages several writes touch (the tail of `posts`, of the likes table, of the
indexes on hot posts) go to the log once instead of once each. Locally (under a 35 % CPU cap,
260,000 users), turning the window off cost 19 % more CPU per request and took p99 from 270 ms
to 1.65 s. Each response still
waits for the commit of its own write (rule 6), and a write never waits longer than the window
plus one pass, or a checkpoint hold (below).

## SQLite

- **Reads walk SQLite's own B-tree cursors** inside the pass's transaction (only the bytecode
  engine is skipped; pages come through SQLite's pager, WAL and page cache), as the organizer
  allowed on 2026-10-31. `sqlite.c` moves a cursor and copies the row's record bytes into
  Goose; Goose decodes them and decides what to read next.
  - **Feed:** walk `posts_created_at_id_idx` from its first entry (it is `created_at DESC, id
    DESC`, so this is exactly the reference `ORDER BY`), then the 20 posts by rowid in
    ascending order (consecutive ids are the next cell of the same leaf), the 20 authors in
    `users`, and the like counts: one range of `likes_post_id_idx` counted in place on its leaf
    pages when the ids are within 512 of each other, a seek per post otherwise.
  - **Post:** a rowid seek on `posts`, one on `users`, and a like count.
  - **Nothing about the data is assumed**: not dense ids, not timestamps in id order. A post
    whose user row is missing (which the JOIN would drop) sends the feed to the reference SQL
    query. The column layout of every table and index decoded is checked at startup.
- **Writes are SQL**: `INSERT … RETURNING id, created_at` and `INSERT … ON CONFLICT DO NOTHING`
  (a new like is told from a repeat by `sqlite3_changes`); the like's 404 check is a seek.
- WAL, `synchronous=NORMAL`, `unix-excl` (the WAL index in heap memory, no per-transaction
  file locks), 32 MB page cache, `mmap_size=0`, `wal_autocheckpoint=0`.
- **One write per commit**: the connections open through "goose", a VFS over unix-excl (SQLite's
  public VFS API). The database file is opened straight through. For the log, writes that
  continue exactly where the previous one ended (a transaction's frames, the header after a
  restart) are gathered, and written the moment the commit frame's page arrives: inside
  SQLite's own write call, so before the frames enter the WAL index and before COMMIT returns,
  when unix-excl would have finished writing them anyway. Any other write (a checksum rewrite),
  and every read, sync, truncate, size query, file control and close, first writes out what is
  gathered. A run goes out in pieces under the unix VFS's 128 KB limit per call. A kill -9 test
  (16 writers, killed at a random moment, 8 rounds, 63,000 acknowledged posts and 63,000
  acknowledged likes) found every acknowledged write in the database and `integrity_check` ok.

## Checkpoints

A thread on its own connection, at nice 19 (so it runs in the loop's idle time), runs a PASSIVE
checkpoint whenever `app.goose` asks; the policy is Goose's:

- Past 16,384 log frames, ask for one. When it finishes, hold writes (reads go on) and ask for
  another, which copies the frames written meanwhile; when that one has copied the whole log,
  the next write starts the log over. A hold lasts at most 150 ms.
- If the log passes 32,768 frames while a checkpoint runs (the loop leaving the thread no idle
  time), writes are held too, and the loop gives the CPU away for 5 ms of every 50 until the
  log is back under, so an overload cannot grow the log without bound.
- The loop never runs a checkpoint itself.

## HTTP, JSON, JWT

- Only the headers the API uses are read. Requests split across receives are completed from
  the connection's box; pipelined requests are answered in order. Heads over 16 KB, bodies over
  1 MB, chunked bodies and malformed request lines get `400 bad request` and the connection
  closes (out of scope in SPEC). `Connection: close` and HTTP/1.0 close after the response.
- HS256 only; every token's signature is checked on every request (constant-time compare);
  only the key's padded blocks are hashed once at startup (the key schedule, not a result).
  Only the canonical base64url spelling of a signature is accepted. `exp` is required and must
  be in the future, then `sub` must be a positive integer string and `username` a string.
- The JSON reader validates whole documents (any nesting, with an explicit stack so no input
  can recurse), decodes escaped member names, and the last duplicate key wins, as in
  `JSON.parse`. `body` is trimmed with JavaScript's `trim()` whitespace set and counted in code
  points (the unit of the schema's `CHECK (length(body) …)`).

## Build

`install.sh` (root, once): `build-essential cmake ninja-build git curl unzip python3`, then the
Goose compiler from its pinned source. It is one large C++20 translation unit; built with one
job at `-O2` and a tighter gcc collector it peaks at 1.34 GB (measured under a 1.6 GB cgroup
cap). It fit on the box for the scored run.

`build.sh` (user): fetches and checks SQLite and liburing, compiles `app.goose` to C
(`goose -O2 --standalone`), and builds `bin/server` static at `-O3` (`-march=x86-64-v3
-mtune=cascadelake`, 256-bit vectors, Ubuntu's default hardening off). **Profile-guided**: an
instrumented build serves `train/train.py`, which makes a synthetic database of its own (5,000
users, 40,000 posts, 160,000 likes, never the seed), signs its own tokens and drives the load
test's request mix plus the test suite's error paths. `PGO=0 bash build.sh` skips it. The
SIGTERM-to-`exit()` hook that writes the profile (`train/pgo_exit.c`) is linked into the
instrumented build only.

The Goose compiler fetches the header-only `goose-simd` package at the commit pinned in
`app.goose`. Its headers inline into the generated C; no extra native library is linked.
The pin is after the module rename to `simd_bytes` (the `v0.2.0` tag predates that rename).
GCC 13 reports attribute-mismatch warnings on the package's `goosimd_bits16/32` declarations;
these are not suppressed. The native build and correctness checks below pass with this pin.

## Rules

1–3. SQLite file at `$SQLITE_PATH`, in-process; schema untouched; reads through SQLite's B-tree
cursors and pager, writes in SQL.
4. `test/test.sh`: 42/42.
5. No caching across requests: every request reads its rows from SQLite while it is served.
Kept between requests: prepared statements, cursor objects, `KeyInfo`, the four root page
numbers (schema metadata read at open) and the HMAC key schedule. Scratch buffers and boxes are
reused as storage only.
6. WAL + `synchronous=NORMAL`; group commit; each response waits for the commit of its pass.
7. Nothing detects the load test; write coalescing applies to every write, whatever its route.
8–9. Direct on `HOST:PORT`.
10. Built from source on the box; Goose, SQLite and liburing pinned by commit/version and sha256.
11. Nothing on the box is tuned; the process raises its own soft `RLIMIT_NOFILE` to the hard limit.

## Local measurements (not a score)

The measurements below predate the header-inline transport change. Use the A/B loop below
to measure changes on your own machine; these historical results are not its baseline.

Not the organizer's box: an Ubuntu 24.04 arm64 VM (Lima on an Apple M4, kernel 6.8.0-142), the
server alone on one vCPU under systemd (`AllowedCPUs=0`, `MemoryMax=1800M`, `LimitNOFILE=65535`,
so 65,535-slot rings as on the box), a native replay of `bench/load.js`'s user model (one
keep-alive connection per user, the same request mix) on the other three vCPUs over loopback,
a fresh seed copy per run, 60 s ramp and 60 s hold. An M4 core is about three times the box's,
so think times are divided by 2 (or more) to put the server's vCPU at the box's utilization.
CPU is the server cgroup's over the hold. rust-iouring-kensac was built from its folder here.

300,000 users at 2.8x the user model's rate (88,500 requests/s offered), past the point where
both scored builds and kensac fail:

| Server | served req/s | p95 | p99 | µs/request | CPU | peak memory |
|---|---:|---:|---:|---:|---:|---:|
| **this version** | **88,622** | **26 ms** | **174 ms** | **8.92** | 79 % | **854 MB** |
| this version with 4,096 x 2 KB receive buffers | 86,117 | 573 ms | 942 ms | 10.04 | 86 % | 1,232 MB |
| rust-iouring-kensac (#264, 310,000 on the box) | 81,395 | 1,540 ms | 3,310 ms | 10.62 | 86 % | 1,781 MB |

At 3.2x this version is past its CPU (99 %) and fails too (p99 1.4 s), so its local ceiling at
300,000 users is about 88,000 requests/s; kensac's and `67ef830`'s are under 70,000 (both fail
at 2.3x).

260,000 users at 2x (55,000 requests/s), where the box failed `204b6b0`: this version 9.3-9.7
µs/request and p99 22-25 ms in quiet runs, `204b6b0` 14.5 µs/request and p99 2,359 ms, kensac
10.6 µs/request and p99 22 ms. Runs here vary: a few seconds where the host deschedules the
VM's vCPU show up as p99 spikes for every server alike.

Behaviour, request for request against rust-iouring-kensac on fresh seed copies: 86
hand-written raw-request cases (pipelining, requests split byte by byte, every token failure,
escapes, surrogate pairs, Unicode trimming, multi-byte 500-character limits, duplicate keys,
nested members, invalid UTF-8, chunked bodies) give byte-identical status and body except one,
a body nested 300 arrays deep, which this entry accepts (as `JSON.parse` does) and kensac
rejects. A ring-spill run with 2,000-slot tables (`GOOSE_SLOTS`, test only) served 10,000 users
across six rings with no errors.

Test-only knobs, never set on the box: `GOOSE_SLOTS`, `GOOSE_WRITE_US`, `GOOSE_CK_FRAMES`,
`GOOSE_PLAIN_VFS` (plain unix-excl, for the comparison above) and `GOOSE_STATS` (a line every
second: passes, the longest pass split into serving and commit, completions per pass, buffer
exhaustion and re-arms, boxes in use, the log, gates and restarts).

## Quick A/B loop

Run from the repository root on Linux. Requires Python 3, GCC/make/Perl (to build wrk),
curl, jq, OpenSSL, and a kernel allowing io_uring. Install the pinned Goose compiler once
with `install.sh`, or set `GOOSE_BIN=/path/to/goose/build/goose` to use a compatible source
build. The source tree must accompany that compiler.

```sh
s=submissions/goose-iouring-ahmadsh2007
# Once, only if the seed does not already exist:
test -f seed/feed.db || bash seed/make-seed.sh
bash "$s/bench/setup.sh"                 # builds hash-checked wrk 4.2.0 locally
PGO=0 bash "$s/build.sh"
cp "$s/bin/server" "$s/.build/baseline"  # preserve BEFORE editing

# After one change:
PGO=0 bash "$s/build.sh"
python3 "$s/bench/compare.py" "$s/.build/baseline" "$s/bin/server" \
  --output "$s/.build/comparison.json"
```

The default comparison takes about two minutes after compilation: three alternating A/B
rounds of feed-only, post-only and mixed traffic, five measured seconds each plus one second
of warm-up. Both binaries first pass `test/test.sh` and fragmented/pipelined golden reads.
Each run gets a fresh seed copy, fresh JWTs, 256 keep-alive connections and a 65,535 descriptor
limit. The server (including its checkpoint thread) uses one CPU; wrk uses up to four other
CPUs, excluding the server's SMT sibling. No system settings are changed. Server `GOOSE_*`
test knobs are cleared so shell settings cannot silently change one experiment.

Results include requests/s, p95/p99, process CPU µs/request, utilization, binary/seed/load
hashes, raw output and validation results. Any socket, HTTP-status or response-shape error
fails the comparison. Response-shape checks during load are not a complete JSON validator;
the separate API checks compare golden bytes and write/read semantics.

For a roughly 20-second targeted check, add `--workloads post --rounds 2 --seconds 3`.
For confirmation, use `--rounds 6 --seconds 10`, then rebuild **both** variants with the
default PGO and repeat. Never compare a PGO candidate against a non-PGO baseline. Use the
same compiler and flags for both; the Goose compiler update itself is a separate experiment.

First compare the baseline binary against itself to learn the machine's noise floor.
Keep other builds/benchmarks off these cores (including SMT siblings); override placement
with `--server-cpu 6 --client-cpus 1,2,3,4` when needed. Median changes and each paired CPU
change are printed. A difference comparable to the A/A variation is inconclusive. The
below-85%-CPU warning means throughput may be limited by the client or batching latency;
do not interpret it as server capacity. CPU cost still depends on batching and workload.
This is a closed-loop optimization diagnostic, not the challenge's many-connection score
or an open-loop tail-latency test. Mixed requests follow the challenge's relative weights,
but omit think times and use a synthetic short post body.

### First transport experiment: inconclusive speedup

On 2026-11-10, on a shared Ryzen 9 3950X/Linux 6.8 host, both variants were built with
Goose `74c641c5`, GCC 13.3 and PGO. Four alternating pairs per workload, six measured seconds
per run, server CPU 6 and client CPUs 1–4 gave these median process CPU costs:

| Workload | Separate transport | Header-inline transport | Change |
|---|---:|---:|---:|
| feed | 14.169 µs/request | 14.164 µs/request | ≈0% |
| post | 9.380 µs/request | 9.299 µs/request | −0.9% |
| mixed | 12.187 µs/request | 12.020 µs/request | −1.4% |

These are **not confirmed improvements**. A subsequent three-pair A/A control on the same
binary showed median CPU differences of +0.2%, −5.1% and −0.3%, respectively; individual
pairs in the A/B run varied substantially too. Other activity on this shared host can change
frequency and contention even with CPU affinity. Do not infer a capacity score from these
numbers. Both variants passed all 42 API checks, fragmented/pipelined golden reads and all
load checks in both PGO and non-PGO comparisons. Assembly inspection found 21 direct call
sites to `box_len`, `rx_take`, `op_send`, `box_put2` and `ring_reap` in the PGO baseline and
none in the header-inline candidate. The optimization is real; its end-to-end benefit still
needs a quieter measurement. The header change is retained as the first A/B candidate, not
as a demonstrated performance win.

### JSON scanning: measured encoder improvement

The next experiment (2026-11-10, same host/compiler) replaced the hand-written eight-byte
JSON-special-character scan with `std::find_any` over a byte set containing controls, quote
and backslash. The standard scanner uses SIMD where available. Encoding and JSON input
parsing share this scan; neither their semantics nor the buffer ownership changed. No new
library or unsafe borrowed view was needed.

Hardware profiling was denied by the host's perf permissions. Temporary binaries linked
with a 1 ms user-CPU timer sampler instead identified escaping as about 16% of Goose's
user-space samples on single-post reads (Rust: about 10%). This is a sampling estimate, not
a call-tree profile. `/proc` charged about 95% of CPU time to the kernel in these loopback
runs, so application-only improvements need separate measurement from total request cost.
All throughput comparisons below used ordinary binaries without the sampler.

An isolated benchmark compiled the actual before/after encoder source with matching GCC
optimization and architecture settings, without PGO. Ten million calls per size, varying
input alignment, three runs each, gave these median times for strings needing no escapes:

| String bytes | Before | After |
|---|---:|---:|
| 24 | 8.44 ns | 6.88 ns |
| 128 | 25.07 ns | 12.36 ns |
| 500 | 85.74 ns | 34.40 ns |

The full PGO servers, four alternating pairs of six seconds per workload, gave:

| Workload | Before req/s | After req/s | Before CPU µs/req | After CPU µs/req |
|---|---:|---:|---:|---:|
| feed | 70,652 | 72,312 | 14.040 | 13.688 |
| post | 106,019 | 106,847 | 9.358 | 9.309 |
| mixed | 81,415 | 82,287 | 11.982 | 11.918 |

Feed CPU cost improved 2.5–2.7% in every pair; a subsequent identical-binary control varied
from −0.9% to +0.6% on feed CPU cost. Single-post CPU cost initially improved 5.5% over six
ten-second pairs, but only 0.5% in the repeat above, within the control's −2.1% to +0.5%
paired variation. **The encoder improvement and local feed benefit are supported; a
single-post or mixed HTTP speedup is not established.** These are not capacity scores.

Both servers passed the 42 API checks, fragmented/pipelined golden reads and all load
checks. Additional temporary validation checked 549,120 byte/position combinations around
8- and 16-byte boundaries, plus 842 HTTP cases per binary covering escaped controls,
quote/backslash, Unicode, rejection of raw controls, and a 500-emoji body across receive
buffers. The binaries for that experiment are saved locally as `.build/pre-post-pgo` and
`.build/byteset-pgo`; `bin/server` now contains the subsequent SIMD experiment below.

### goose-simd versus goose-bstr

The current JSON scanner uses `goose-simd` vectors: compare quote/backslash and mask off the
upper three bits to identify control bytes, then combine the masks. It checks 32 bytes at a
time, uses 16-byte vectors for shorter inputs, and stays inside the slice for overlapping
tail loads. An early check avoids vector setup when the next byte already needs escaping.
The same scanner serves JSON input parsing and output encoding. Ownership is unchanged.

Compared with the standard byte-set version above, three native microbenchmark runs gave
these median encoder times (nanoseconds per call; varying alignment, one million calls):

| Input | Standard byte set | Specialized goose-simd |
|---|---:|---:|
| 128 plain bytes | 12.58 | 9.69 |
| 500 plain bytes | 34.67 | 26.97 |
| 2,000 plain bytes | 115.86 | 82.86 |
| 500 quotes | 2,027 | 846 |

[`goose-bstr`](https://github.com/sstadick/goose-bstr/commit/436c8a950ca6c0b3bc59d3b195881100c7bc99de)
was also tested. Its generic 34-byte escape set builds a table and scans scalarly: about
197 ns for 500 plain bytes here. Its newline search helped longer
HTTP headers, but was not consistently faster on short headers. A `goose-simd` newline-scan
experiment improved the isolated parser but did not establish an end-to-end benefit, so
HTTP framing keeps the standard scanner. No `goose-bstr` dependency was added.

End-to-end PGO confirmation (four eight-second pairs) reduced feed CPU/request from 13.660
to 13.523 µs, about 1%; an earlier four-pair run measured 0.7%. Single-post CPU changes
reversed from −2.1% to +3.2% between runs. Identical-binary controls varied from −6.6% to
+10.6% per pair on single-post CPU cost. **The isolated encoder gain is repeatable; do not
claim a single-post server speedup or regression from these noisy loopback measurements.**

Validation: all 42 API checks and load checks passed. The scanner passed 3,877,120
byte/position/alignment cases on AVX2, SSE2 and the package's scalar backend. Both the
byte-set baseline and the retained server passed 846 JSON roundtrip/rejection cases and
70 fragmented-header cases, including long Unicode and quote/backslash-heavy bodies.
Use `.build/byteset-pgo` versus `bin/server` for this experiment's short-cycle A/B comparison.

### SQLite through c_headers: tested, not retained

An experiment on the same host/compiler moved `sqlite.c` to `sqlite.h`, included it through
`c_headers`, used Goose's generated ABI types, and made the adapters `static inline`. The
unmodified amalgamation was compiled once, together with Goose, with the same SQLite options.
`_GNU_SOURCE` had to be defined on the compiler command line before Goose's libc includes.
Row copies, SQL, cursor operations, transaction policy and checkpointing were unchanged.

PGO disassembly showed 35 direct call sites to the inspected cursor/transaction/checkpoint
helpers before, and four after (two each to `db_begin` and specialized `db_seek_ge`). However,
four alternating six-second HTTP pairs did not establish a useful performance improvement:

| Workload | Separate SQLite CPU µs/request | Header SQLite CPU µs/request |
|---|---:|---:|
| feed | 13.520 | 13.496 |
| post | 9.293 | 9.093 |
| mixed | 11.710 | 11.800 |

Single-post paired CPU changes were −5.9%, −4.6%, +0.4% and +0.3%, within the earlier
identical-binary control's variation. A second HTTP run overlapped compilation and was
discarded for performance inference, rather than treating contention as a code regression.

A temporary no-network harness called the actual generated `get_post` handler, including
SQLite reads, JSON encoding and response-box copying, committing every 256 calls. Six
alternating pairs of two million calls, after warm-up, with matching non-PGO builds gave
median process CPU times of **252.3 → 250.8 ns** for repeated post 1 and **2,442.5 → 2,456.3 ns**
for a permutation across the first 250,000 post IDs. Response checksums matched. These mixed,
sub-1% differences do not justify keeping the header conversion as a performance change.

Both PGO variants passed all 42 API checks, fragmented/pipelined golden reads and load checks.
Additional synthetic databases (512-byte pages, verified overflow pages, long usernames,
500-emoji bodies, sparse/large IDs, multi-page likes and an orphan post forcing SQL fallback)
matched independently computed SQL results with both variants and the non-PGO candidate.
Created posts and idempotent likes persisted, and `integrity_check` returned `ok`.

The working source and `bin/server` retain separate SQLite compilation. Local experiment
binaries remain as `.build/pre-sqlite-pgo` and `.build/sqlite-header-pgo`; the usual A/B command
can compare them without rebuilding. Raw measurements and a re-applicable candidate patch
are saved under the repository's ignored `.amp/in/artifacts/sqlite-*` paths. No zero-copy
borrowing was introduced; removing calls alone was not a meaningful win here.

### Scattered-post profile: cache misses and B-tree searches, not row copies

The seed has **500,000 posts**. The challenge picks a post from the latest feed; scattered
reads are a separate working-set stress test, not the challenge's normal post distribution.
A temporary harness called the unchanged generated `get_post` handler without networking,
including record decoding, JSON and response-box copying, committing every 256 requests.
Matching GCC 13 non-PGO builds ran on CPU 6. Each scattered/sequential run first traversed
its entire ID range; hot controls warmed up for 10,000 calls. Three alternating cache-size
pairs of one million measured requests gave these median process CPU times:

| Post selection | 32 MiB cache µs/post | 128 MiB cache µs/post | SQLite misses/post, 32 → 128 MiB |
|---|---:|---:|---:|
| repeated post 500000 | 0.265 | 0.264 | 0 → 0 |
| latest 20, round-robin | 0.397 | 0.397 | 0 → 0 |
| sequential first 250k | 0.640 | 0.633 | 0.054 → 0 |
| scattered first 250k | 2.638 | 1.814 | 0.646 → 0 |
| scattered all 500k | 4.118 | 2.085 | 1.903 → 0 |

Scattered IDs follow a full permutation with stride 7,919. The larger cache reduced CPU
cost by 28–33% in every 250k pair and 49–54% in every 500k pair. Actual SQLite cache usage
grew from 32.9 to 58.4 MiB for the 250k range, and to 114.3 MiB for all 500k. A separate
8/32/64/128 MiB sweep of all 500k produced 2.214/1.903/1.081/0 misses per post; timings at
intermediate sizes were not monotonic, so miss count alone does not predict CPU cost.

Temporary adapter timers and a SQLite VFS `pread` hook attributed the work in two additional
500,000-request runs per case. For scattered 250k at 32 MiB, inclusive time per post was:

| Operation | Instrumented µs/post | SQLite misses/post |
|---|---:|---:|
| post-table seek | 1.26–1.58 | 0.5866 |
| author-table seek | 0.31–0.41 | 0.0090 |
| fast likes-index count | 0.81–1.01 | 0.0507 |
| post/author record fetch and copy, combined | 0.087–0.108 | 0 |

Likes fallback adds about 0.03–0.04 µs/post. Record copies, including fallback records, total
about 210 bytes/post. Post-table seeks account for 91% of the 250k case's SQLite misses.
Across all 500k, each post seek misses once and likes counting adds about 0.88 misses/post.
With a 128 MiB cache, misses disappear but scattered reads remain much slower than hot reads:
post/author seeks and likes-index work still dominate the instrumented database time.

These stage times include timer overhead (19–20 ns minimum per timed interval), nested read
timers, and host variation. They are attribution estimates, not uninstrumented timings or
additive speedup predictions. `pread` time is already included in the seeks/counts above.
Public SQLite cache totals exactly matched the per-stage totals, and counted `pread` calls
matched misses. **No block-device read bytes or major faults were recorded** in any measured
run: the OS cached the file even when SQLite evicted its own parsed pages. Hardware perf
counters remain unavailable; SQLite misses here are not CPU-cache miss measurements.

All 4,962 sampled responses matched independent SQL queries; response-length checksums
matched across cache sizes. The read-only latest-20 control sees no benefit from a larger
cache. Before spending another ~81 MiB under the challenge's memory cap, test the realistic
mixed workload with writes and checkpoints. No production cache setting or server code was
changed. Raw counters, timings and commands are in the ignored
`.amp/in/artifacts/sqlite-profile-results.json`; temporary profiling code was removed.

## What safe zero-copy borrowing would require

This investigation targets Goose `74c641c5`, not a proposed language feature already present.
Its [FFI contract](https://aardappel.github.io/goose/docs/reference.html#24-c-interop) accepts
slices as arguments, rejects slice returns from `extern fn`, and forbids C retaining borrowed
arguments beyond a call. A compiler probe of `extern fn row_view(cursor: i32) -> const u8[:]`
fails at typechecking. `ffi::bytes` copies foreign bytes. `c_headers` changes optimization
visibility, not those lifetime rules. goose-io's borrowed lines instead reference a
Goose-owned reader buffer filled synchronously; goose-bstr searches existing Goose slices.

There is already a zero-copy route in the **opposite direction**: a C host can pass its
buffer to an `export fn` taking `const u8[:]`, on the thread that initialized Goose. A small
C-host probe verified pointer identity through an exported echo function and correct
checksums before and after changing the C buffer between calls. The C caller must keep
the bytes valid for the call and honor any returned view's lifetime. This could support a
C-hosted parser/decoder, but is not a drop-in borrowed return to this Goose-owned loop.
Calling an export back from an extern also needs a reentrancy/effect audit; the probe did
not test that arrangement or asynchronous buffer retention.

Keeping the event loop and record decoding in Goose would need these additional contracts:

* **Scoped foreign views with an owner.** A trusted adapter supplies a pointer and length;
  Goose tracks the resulting view back to an opaque owner/lease and prevents escape. Merely
  allowing pointer casts or slice returns with no owner would not be safe.
* **Invalidation effects, not just owner lifetime.** A live SQLite cursor does not guarantee
  that yesterday's payload pointer remains valid. Cursor movement, transaction end and any
  other operation that can invalidate that payload must be forbidden while it is borrowed.
  A conservative first API would permit no SQLite calls during a payload-view scope and
  copy overflow-page records. Feed text needed after moving cursors still needs copying or
  an independently pinned owner. Goose currently permits aliasing, so adding a root alone
  does not enforce these exclusions through aliases.
* **Receive leases.** A selected io_uring buffer can be viewed after its completion and
  before it is returned to the provided-buffer ring. Return/republication must invalidate
  access and happen exactly once. Partial requests and held writes must either copy their
  bytes or retain leases, with a bound on buffers withheld from receiving. Completion-ring
  entries themselves are invalid after advancing the CQ head.
* **Asynchronous ownership transfer.** A send must own stable, immutable bytes until the
  operation completes; a receive destination must be inaccessible while the kernel writes.
  No resize, reuse, close or scope exit may free that storage early. Cancellation submission
  is not completion; partial sends and stale completion generations need explicit handling.
  `SEND_ZC`, if introduced later, also needs its buffer-release notification, not merely
  the initial send completion. This is distinct from removing the user-space staging copy.

A narrowly scoped foreign-view API is a smaller first language feature than general async
borrowing. Its tests should reject escaping views, cursor movement/close through aliases
while borrowed, reuse during an outstanding operation, and double release. Runtime tests
must cover overflow records, buffer starvation, partial I/O, cancellation and slot reuse.
The compiler/FFI would also need correct effects for callbacks into Goose; arbitrary C
callbacks cannot secretly mutate globals that the optimizer assumes unchanged.

The transport inlining in this submission deliberately changes none of these contracts.

## Credit

Ideas from earlier entries, all MIT, with the code written for this entry: direct descriptors
in several rings, single-shot accepts kept in flight over `SO_REUSEPORT` listeners, multishot
receives into provided buffers, the buffer-ring retry for Ubuntu's kernels, and holding writes
while a checkpoint copies the log's tail come from rust-iouring-kensac (#264); counting likes
on the index's leaf pages, locking the code in memory, hardening-off flags and PGO on a
synthetic database from c-iouring-pavlyhalim (#245) and mojo-iouring-oh-meh (#257); reading
through SQLite's B-tree cursors from lean-derive-hargup (#153), as the organizer ruled.

## Files

| | |
|---|---|
| `app.goose` | the server: the loop and connection table, accepts and rings, holds, coalescing, checkpoint policy, HTTP, routing, JWT/SHA-256/HMAC, JSON, record decoding, read plans |
| `uring.h`, `app.build` | header-inline kernel adapters: rings, queued operations, completions, receive buffers, byte boxes, listeners |
| `sqlite.c` | SQLite: the amalgamation, cursor moves and row copies, the pass's transaction, writes, the checkpoint thread |
| `train/train.py`, `train/pgo_exit.c` | PGO training (build time only) |
| `bench/setup.sh`, `bench/compare.py`, `bench/load.lua` | local A/B tool setup, correctness gates, CPU/throughput comparisons |
| `install.sh`, `build.sh`, `start.sh` | |
