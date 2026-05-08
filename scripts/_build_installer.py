"""mddbtest/install.ps1 생성 — wheel base64 embed 올인원 installer.

사용:
    cd MDDB
    python -m build               # ../mddbtest/dist/ 또는 MDDB/dist/ 에 wheel
    python scripts/_build_installer.py [--wheel <path>] [--out <path>]

기본:
    --wheel  C:/Users/aa/Documents/AIProjects/MDDB/dist/mddbai-*.whl (가장 최신)
    --out    C:/Users/aa/Documents/AIProjects/mddbtest/install.ps1
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path


HEADER = '''# mddbtest 올인원 설치 파일 (PowerShell)
#
# 사용:
#     .\\install.ps1
#
# 동작 (멱등):
#   1) .venv 가상환경 (없으면) 생성 — 격리
#   2) 동봉된 wheel 디코딩 -> .venv 에 설치
#   3) .mddbai 가 없으면 mddbai init 호출
#
# 올인원 — 이 파일 1 개 안에 wheel 162 KB 통째 embed.
# 외부 의존 0 (Python 3.11+ 만 있으면 됨).

$ErrorActionPreference = "Stop"
chcp 65001 > $null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# ---- embedded wheel (base64) ----
$WheelBase64 = @'
'''

FOOTER_TPL = '''
'@
# ---- end embedded wheel ----

# 모든 mddbai 자료 한 자리 (.mddbai/) — venv (.venv) + 데이터를 같은 .mddbai 안에.
$mddbaiRoot = Join-Path $PSScriptRoot ".mddbai"
$venvPath   = Join-Path $mddbaiRoot ".venv"
$venvPython = Join-Path $venvPath "Scripts\\python.exe"
$venvMddbai = Join-Path $venvPath "Scripts\\mddbai.exe"

if (-not (Test-Path $mddbaiRoot)) {
    New-Item -ItemType Directory -Path $mddbaiRoot | Out-Null
}

$wheelPath = Join-Path $mddbaiRoot "{WHEEL_NAME}"
[System.IO.File]::WriteAllBytes($wheelPath, [System.Convert]::FromBase64String($WheelBase64))

if (-not (Test-Path $venvPython)) {
    Write-Host "[1/4] venv:     생성 중 (.mddbai/.venv)"
    python -m venv $venvPath
} else {
    Write-Host "[1/4] venv:     이미 있음 (.mddbai/.venv)"
}

Write-Host "[2/4] install:  embedded wheel 깔기"
# Defensive: 옛 'mddb' 패키지 잔재 제거 (옛 mddb.exe entry script 가 새 mddbai 와
# 충돌해서 ModuleNotFoundError 일으킴). uninstall 실패 (패키지 없음) 는 무시.
& $venvPython -m pip uninstall -y mddb 2>$null | Out-Null
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet --force-reinstall $wheelPath

Remove-Item $wheelPath
Write-Host "[3/4] cleanup:  임시 wheel 제거"

# [4/4] init — idempotent. 매번 실행해서 최신 hooks/skills/settings 반영.
# 기존 _AGENT_GUIDE.md / 사용자 hooks / 다른 skills / 다른 settings.json 항목 보존.
Write-Host "[4/4] palace + claude integration:  mddbai init .mddbai --with-claude-hook (idempotent)"
& $venvMddbai init $mddbaiRoot --with-claude-hook | Out-Null

Write-Host ""
Write-Host "DONE. 사용 시작:"
Write-Host "    .\\.mddbai\\.venv\\Scripts\\Activate.ps1"
Write-Host "    mddbai doctor .mddbai"
'''


def latest_wheel(repo_dist: Path) -> Path:
    cands = sorted(repo_dist.glob("mddbai-*.whl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not cands:
        sys.exit(f"wheel 못 찾음: {repo_dist}/mddbai-*.whl. 'python -m build' 먼저.")
    return cands[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--wheel",
        type=Path,
        default=None,
        help="wheel 경로. 미지정 시 ../MDDB/dist/mddbai-*.whl 가장 최신",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("C:/Users/aa/Documents/AIProjects/mddbtest/install.ps1"),
        help="install.ps1 출력 경로",
    )
    args = ap.parse_args()

    if args.wheel is None:
        repo_root = Path(__file__).resolve().parents[1]
        args.wheel = latest_wheel(repo_root / "dist")

    data = args.wheel.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    # 76 자 단위 줄바꿈 (PowerShell here-string 가독성)
    wrapped = "\n".join(b64[i : i + 76] for i in range(0, len(b64), 76))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    footer = FOOTER_TPL.replace("{WHEEL_NAME}", args.wheel.name)
    with args.out.open("wb") as f:
        f.write(b"\xef\xbb\xbf")  # UTF-8 BOM (PowerShell 5 한글 인식)
        f.write(HEADER.encode("utf-8"))
        f.write(wrapped.encode("ascii"))
        f.write(footer.encode("utf-8"))

    out_size = args.out.stat().st_size
    print(f"wheel:        {args.wheel} ({len(data):,} B)")
    print(f"base64:       {len(b64):,} B ({wrapped.count(chr(10)) + 1} 줄)")
    print(f"installer:    {args.out} ({out_size:,} B)")


if __name__ == "__main__":
    main()
