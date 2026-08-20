--[[
  replace.lua  –  JSON-driven text replacement for IDOL Windows setup

  Usage (run from the project root that contains the "config" folder):

      lua tools/replace.lua tools/replacements.json

  What it does:
    1. Applies every from→to replacement listed in the JSON to the listed files
       (creates .bak backups for any file that actually changes).
    2. At the end, updates config/my-config.json  (and default-config.json if present)
       so the Ports / LicensePort values match the *target* ports from the JSON.

  The script uses the dkjson.lua that already ships with the project.
]]

-- ---------- helpers ----------
local function load_dkjson()
    local candidates = {
        "config/cfg/answerserver/rag/dkjson.lua",
        "idol-windows-setup-main/config/cfg/answerserver/rag/dkjson.lua",
        "dkjson.lua",
        "tools/dkjson.lua"
    }
    for _, path in ipairs(candidates) do
        local ok, mod = pcall(dofile, path)
        if ok and mod then return mod end
    end
    error("Could not find dkjson.lua. Run this script from the project root.")
end

local json = load_dkjson()

local function read_file(path)
    local f, err = io.open(path, "rb")
    if not f then return nil, err end
    local data = f:read("*a")
    f:close()
    return data
end

local function write_file(path, data)
    local f, err = io.open(path, "wb")
    if not f then return false, err end
    f:write(data)
    f:close()
    return true
end

local function process_entry(entry, base_dir)
    local path = entry.file
    if base_dir and base_dir ~= "" and base_dir ~= "." then
        path = base_dir:gsub("[/\\]+$", "") .. "/" .. path
    end

    local content, err = read_file(path)
    if not content then
        print(string.format("[ERROR] Cannot read %s : %s", path, tostring(err)))
        return false
    end

    local original = content
    local total = 0

    for _, rep in ipairs(entry.replacements or {}) do
        local from = rep.from
        local to   = rep.to or ""
        -- literal replace (escape magic chars)
        local pattern = from:gsub("(%W)", "%%%1")
        local n
        content, n = content:gsub(pattern, to)
        total = total + (n or 0)
    end

    if content == original then
        print(string.format("[SKIP]  %s  (no changes)", path))
        return true
    end

    os.rename(path, path .. ".bak")
    local ok, err = write_file(path, content)
    if not ok then
        print(string.format("[ERROR] Cannot write %s : %s", path, tostring(err)))
        os.rename(path .. ".bak", path)
        return false
    end

    print(string.format("[OK]    %s  (%d replacement(s))", path, total))
    return true
end

-- Extract a numeric port from a "Key=1234" style target string
local function extract_port(s)
    if not s then return nil end
    local num = s:match("=(%d+)%s*$") or s:match("(%d+)%s*$")
    return num
end

-- Map file + key → the Ports key used in my-config.json
-- Priority: more specific files win.
local PORT_MAP = {
    -- Content
    ["config/cfg/content/content.cfg|Port"]          = "Content",
    -- Category
    ["config/cfg/category/category.cfg|Port"]        = "Category",
    -- Community (often shares 9030)
    ["config/cfg/community/community.cfg|Port"]      = "Community",
    -- Agentstore family
    ["config/cfg/agentstore/agentstore.cfg|Port"]    = "Agentstore",
    ["config/cfg/qmsagentstore/agentstore.cfg|Port"] = "QMSAgentStore",
    ["config/cfg/answerbank-agentstore/agentstore.cfg|Port"] = "AnswerBankAgentStore",
    ["config/cfg/answerbankagentstore/agentstore.cfg|Port"]  = "AnswerBankAgentStore",
    ["config/cfg/conversation-agentstore/agentstore.cfg|Port"] = "ConversationAgentStore",
    ["config/cfg/conversationagentstore/agentstore.cfg|Port"]  = "ConversationAgentStore",
    -- QMS / AnswerServer / Stats / View
    ["config/cfg/qms/qms.cfg|Port"]                  = "QMS",          -- the 16000 one is preferred later
    ["config/cfg/answerserver/answerserver.cfg|Port"]= "AnswerServer",
    ["config/cfg/statsserver/statsserver.cfg|Port"]  = "StatsServer",
    ["config/cfg/view/view.cfg|Port"]                = "View",
    -- License
    ["config/cfg/agentstore/idol.common.cfg|LicenseServerACIPort"] = "LicenseServer",
    ["config/cfg/content/idol.common.cfg|LicenseServerACIPort"]    = "LicenseServer",
}

-- Prefer the canonical high-level ports when multiple candidates exist
local PREFERRED = {
    QMS = "16000",          -- ignore the secondary 9100/9030/9150 lines in qms.cfg
    Category = "9020",
    Community = "9030",
}

local function collect_target_ports(data)
    local ports = {}          -- Ports key → number (string)
    local license_port = nil

    for _, entry in ipairs(data) do
        local file = entry.file or ""
        for _, rep in ipairs(entry.replacements or {}) do
            local to = rep.to or ""
            local key = to:match("^([%w_]+)%s*=")
            if key then
                local map_key = file .. "|" .. key
                local ports_key = PORT_MAP[map_key]
                local num = extract_port(to)
                if ports_key and num then
                    -- keep preferred value if already set and this one is secondary
                    if not ports[ports_key] then
                        ports[ports_key] = num
                    elseif PREFERRED[ports_key] and num == PREFERRED[ports_key] then
                        ports[ports_key] = num
                    end
                end
                if key == "LicenseServerACIPort" and num then
                    license_port = num
                    ports["LicenseServer"] = num
                end
            end
        end
    end

    -- Hard-wire a few well-known defaults if they never appeared
    ports.Content               = ports.Content               or "9100"
    ports.Category              = ports.Category              or "9020"
    ports.Community             = ports.Community             or "9030"
    ports.Agentstore            = ports.Agentstore            or "9050"
    ports.QMSAgentStore         = ports.QMSAgentStore         or "9150"
    ports.AnswerBankAgentStore  = ports.AnswerBankAgentStore  or "9450"
    ports.ConversationAgentStore= ports.ConversationAgentStore or "9550"
    ports.QMS                   = ports.QMS                   or "16000"
    ports.AnswerServer          = ports.AnswerServer          or "12000"
    ports.StatsServer           = ports.StatsServer           or "19870"
    ports.View                  = ports.View                  or "9080"
    ports.LicenseServer         = ports.LicenseServer         or "20000"
    ports.NiFi                  = ports.NiFi                  or "8443"
    ports.Find                  = ports.Find                  or "8080"

    return ports, license_port or ports.LicenseServer
end

local function update_my_config(base_dir, ports, license_port)
    local candidates = {
        (base_dir ~= "." and base_dir .. "/config/my-config.json") or "config/my-config.json",
        "config/my-config.json",
        "config/default-config.json",
    }

    local updated = 0
    for _, path in ipairs(candidates) do
        local raw = read_file(path)
        if raw then
            local cfg, _, err = json.decode(raw)
            if cfg then
                local changed = false

                if not cfg.Ports then cfg.Ports = {} end
                for k, v in pairs(ports) do
                    if tostring(cfg.Ports[k] or "") ~= tostring(v) then
                        cfg.Ports[k] = tostring(v)
                        changed = true
                    end
                end

                if license_port and tostring(cfg.LicensePort or "") ~= tostring(license_port) then
                    cfg.LicensePort = tostring(license_port)
                    changed = true
                end

                if changed then
                    -- pretty-print with 2-space indent (dkjson supports it)
                    local out = json.encode(cfg, { indent = true })
                    -- backup
                    os.rename(path, path .. ".bak")
                    local ok, werr = write_file(path, out)
                    if ok then
                        print(string.format("[OK]    Updated ports in %s", path))
                        updated = updated + 1
                    else
                        print(string.format("[ERROR] Failed to write %s : %s", path, tostring(werr)))
                        os.rename(path .. ".bak", path)
                    end
                else
                    print(string.format("[SKIP]  %s  (ports already match targets)", path))
                end
            else
                print(string.format("[WARN]  Could not parse %s : %s", path, tostring(err)))
            end
        end
    end
    return updated
end

-- ===================== main =====================
local config_file = arg[1] or "tools/replacements.json"
local base_dir    = arg[2] or "."

local raw, err = read_file(config_file)
if not raw then
    print("Failed to read JSON config: " .. tostring(err))
    os.exit(1)
end

local data, _, jerr = json.decode(raw)
if not data then
    print("JSON parse error: " .. tostring(jerr))
    os.exit(1)
end

print(string.format("Loaded %d file entries from %s\n", #data, config_file))

local ok_count = 0
for _, entry in ipairs(data) do
    if process_entry(entry, base_dir) then
        ok_count = ok_count + 1
    end
end

print(string.format("\nFile replacements finished. %d / %d entries OK.", ok_count, #data))

-- ---- update my-config.json with the target ports ----
print("\nUpdating config/my-config.json with target ports …")
local target_ports, license_port = collect_target_ports(data)

print("  Target Ports:")
local keys = {}
for k in pairs(target_ports) do keys[#keys+1] = k end
table.sort(keys)
for _, k in ipairs(keys) do
    print(string.format("    %-24s = %s", k, target_ports[k]))
end
print(string.format("    %-24s = %s", "LicensePort (top-level)", license_port))

local n = update_my_config(base_dir, target_ports, license_port)
print(string.format("\nDone. %d config file(s) updated with target ports.", n))
print("Backups created as *.bak next to every modified file.")
