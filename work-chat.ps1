# work-chat.ps1 - launch OpenClaude as a local coding agent through this bridge.
#
#   .\work-chat.ps1                 # uses copilot-work on http://127.0.0.1:8000
#   .\work-chat.ps1 -Model copilot  # personal-account model instead
#   .\work-chat.ps1 -Workspace D:\SpiralPulse
#
# Start the bridge first in another terminal:  python app.py
#
# This intentionally leaves OpenClaude's local tools enabled. The bridge turns
# Copilot's text tool requests into OpenAI tool_calls, and OpenClaude executes
# Read/Glob/Grep/Bash/Edit/Write on this machine.

param(
    [string]$Model     = "copilot-work",
    [string]$BaseUrl   = "http://127.0.0.1:8000/v1",
    [string]$Port      = "8000",
    [string]$Workspace = $PSScriptRoot
)

try {
    $WorkspacePath = (Resolve-Path -LiteralPath $Workspace -ErrorAction Stop).Path
} catch {
    Write-Host "Workspace not found: $Workspace" -ForegroundColor Red
    exit 1
}

# Point OpenClaude at the local OpenAI-compatible bridge.
$env:CLAUDE_CODE_USE_OPENAI = "1"
$env:OPENAI_BASE_URL        = $BaseUrl
$env:OPENAI_API_KEY         = "local"
$env:OPENAI_MODEL           = $Model

# Cowork turns take 30-120s and requests queue behind the bridge's serialized
# upstream lock; OpenClaude's default request timeout (~90s) fires mid-turn and
# triggers retry storms. Give requests 10 minutes.
$env:API_TIMEOUT_MS = "600000"

# Reduce non-essential background model calls; the Copilot backend is serialized.
$env:CLAUDE_CODE_DISABLE_AUTO_MEMORY      = "1"
$env:DISABLE_AUTO_COMPACT                 = "1"
$env:CLAUDE_CODE_DISABLE_BACKGROUND_TASKS = "1"

# Quick reachability check so failures are clear instead of ECONNREFUSED loops.
try {
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:$Port/v1/models" | Out-Null
} catch {
    Write-Host "Bridge not reachable at $BaseUrl - start it first: python app.py" -ForegroundColor Yellow
}

$agentPrompt = @'
You are running in OpenClaude with local coding tools available. When workspace
facts, source code, reports, or filesystem state matter, use the available local
tools instead of guessing. Do not claim you inspected files, directories, or
command output unless a tool result in this conversation shows it.
'@

# Permission bypass keeps coding-agent runs non-interactive. Remove this flag if
# you want OpenClaude to prompt before sensitive local actions.
Write-Host "Launching OpenClaude in workspace: $WorkspacePath" -ForegroundColor Cyan
Push-Location -LiteralPath $WorkspacePath
try {
    $env:PWD = $WorkspacePath
    openclaude --add-dir $WorkspacePath --append-system-prompt $agentPrompt --dangerously-skip-permissions
} finally {
    Pop-Location
}
