-- Closed-loop CPU/throughput diagnostic, not the challenge's capacity score.
-- Mixed weights match bench/load.js: 100 feeds, 100 posts, 15 likes, 2 creates.
local threads, tokens, ids = {}, {}, {500000}
local serial, mode = 0, os.getenv("WORKLOAD")
errors = 0

function setup(thread)
  thread:set("seed", #threads + 42)
  threads[#threads + 1] = thread
end

function init(args)
  math.randomseed(seed)
  local f = assert(io.open(os.getenv("TOKENS"), "r"))
  for token in f:read("*a"):gmatch('"token"%s*:%s*"([^"]+)"') do
    tokens[#tokens + 1] = token
  end
  f:close()
  assert(#tokens > 0)
end

function request()
  if mode == "feed" then return wrk.format("GET", "/feed") end
  if mode == "post" then return wrk.format("GET", "/posts/500000") end
  local n = math.random(217)
  if n <= 100 then return wrk.format("GET", "/feed") end
  local id = ids[math.random(#ids)]
  if n <= 200 then return wrk.format("GET", "/posts/" .. id) end
  local headers = {Authorization = "Bearer " .. tokens[math.random(#tokens)],
                   ["Content-Type"] = "application/json"}
  if n <= 215 then return wrk.format("POST", "/posts/" .. id .. "/like", headers, "") end
  serial = serial + 1
  return wrk.format("POST", "/posts", headers,
    '{"body":"local comparison post ' .. serial .. ': fresh reads and writes"}')
end

function response(status, headers, body)
  if status ~= 200 and status ~= 201 then errors = errors + 1; return end
  if body:sub(1,10) == '{"posts":[' then
    local fresh = {}
    for id in body:gmatch('"id":(%d+)') do fresh[#fresh + 1] = tonumber(id) end
    if #fresh ~= 20 then errors = errors + 1 else ids = fresh end
  elseif body:sub(1,8) ~= '{"post":' and body:sub(1,14) ~= '{"liked":true,' then
    errors = errors + 1
  end
end

function done(summary, latency, requests)
  local semantic = 0
  for _, t in ipairs(threads) do semantic = semantic + t:get("errors") end
  local e = summary.errors
  io.write(string.format(
    'RESULT {"requests":%d,"seconds":%.6f,"p95_us":%d,"p99_us":%d,' ..
    '"errors":%d,"semantic_errors":%d}\n',
    summary.requests, summary.duration / 1e6, latency:percentile(95), latency:percentile(99),
    e.connect + e.read + e.write + e.status + e.timeout, semantic))
end
