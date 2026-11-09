import json
import subprocess
from pathlib import Path

log_path = Path("audit/e2e_demo.log")
lines = []


def run_step(title, cmd):
    lines.append(f"=== {title} ===")
    lines.append(f"CMD: {cmd}")
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, errors="replace")
    out = (res.stdout or "").strip()
    err = (res.stderr or "").strip()
    if out:
        lines.append(out)
    if err:
        lines.append(f"[STDERR] {err}")
    lines.append(f"Exit Code: {res.returncode}\n")


def main():
    log_path.parent.mkdir(parents=True, exist_ok=True)

    run_step("1. Initialize Database Tables", "research db init")
    run_step(
        "2. Import 50 Synthetic Publications via Fixture",
        'research import-openalex --query "HVAC demo" --fixture contracts/papers.jsonl',
    )
    run_step(
        "3. Export 50-Paper Screening Package (research next)",
        "research next --study buildingpulse_fdd --protocol contracts/research_protocol.md --count 50",
    )
    run_step(
        "4. Check Study Status before Review",
        "research status --study buildingpulse_fdd --protocol contracts/research_protocol.md",
    )
    run_step(
        "5. Accept and Validate Screening Results",
        "research accept --file exchange/incoming/results.json",
    )
    run_step(
        "6. Repeat Accept (Idempotent Safe No-op Verification)",
        "research accept --file exchange/incoming/results.json",
    )
    run_step(
        "7. Check Study Status after Review Commit",
        "research status --study buildingpulse_fdd --protocol contracts/research_protocol.md",
    )
    run_step(
        "8. Evaluate Quality against Gold Standard Template",
        "research evaluate-gold --file contracts/gold_set_template.csv --study buildingpulse_fdd",
    )

    log_path.write_text("\n".join(lines), encoding="utf-8")
    print("Demo execution logged to audit/e2e_demo.log")


if __name__ == "__main__":
    main()
