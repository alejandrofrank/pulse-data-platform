param(
    [Parameter(Mandatory = $true)]
    [string]$SecretPath
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security
$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$plainBytes = $null
$process = $null
try {
    $plainBytes = [Security.Cryptography.ProtectedData]::Unprotect(
        [IO.File]::ReadAllBytes($SecretPath), $null,
        [Security.Cryptography.DataProtectionScope]::CurrentUser)
    # Only the static program is passed as an argument. Credential bytes travel
    # over redirected stdin, never a command argument, environment variable or log.
    $program = @'
import os, re, sys, tempfile
from pathlib import Path
key = sys.stdin.buffer.read(4097).strip()
if not re.fullmatch(rb'sk-[A-Za-z0-9_-]{16,1020}', key):
    raise SystemExit('Credential format is invalid')
directory = Path('/run/pulse')
with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
    temporary = Path(output.name)
    output.write(key)
try:
    os.chmod(temporary, 0o400)
    os.replace(temporary, directory / 'openai-api-key')
finally:
    if temporary.exists():
        temporary.unlink()
print('OpenAI credential loaded into memory-backed API storage.')
'@
    $encodedProgram = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($program))
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = 'docker.exe'
    $start.Arguments = "compose exec -T api python -c `"import base64;exec(base64.b64decode('$encodedProgram'))`""
    $start.WorkingDirectory = $repositoryRoot
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    if (-not $process.Start()) { throw 'Cannot start Docker client' }
    $process.StandardInput.BaseStream.Write($plainBytes, 0, $plainBytes.Length)
    $process.StandardInput.Close()
    $outputTask = $process.StandardOutput.ReadToEndAsync()
    $errorTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit(30000)) {
        $process.Kill()
        throw 'Docker credential transfer timed out'
    }
    if ($process.ExitCode -ne 0) { throw 'Docker credential transfer failed' }
    # The controlled helper only prints this fixed completion sentence.
    Write-Output 'OpenAI credential loaded into memory-backed API storage.'
} catch {
    Write-Error 'Unable to load the stored credential. Check the Windows account and running API service.'
    exit 1
} finally {
    if ($plainBytes) { [Array]::Clear($plainBytes, 0, $plainBytes.Length) }
    if ($process) { $process.Dispose() }
}
