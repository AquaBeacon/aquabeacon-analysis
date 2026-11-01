"""Download a named GEONET hourly base and solve the registered Nanyo rover."""

import argparse
import gzip
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path

from ppk_ngt_geonet import Inputs, get, observations

parser = argparse.ArgumentParser()
parser.add_argument("--experiment", required=True)
parser.add_argument("--station", required=True)
args = parser.parse_args()
p = (Path("data/interim") / args.experiment / "gnss").resolve()
target = p / f"base_{args.station}"
target.mkdir(exist_ok=True)
fields = json.loads((p / "request.json").read_text())
fields["sel_obs[0]"] = args.station
page = get("https://terras.gsi.go.jp/hour_download.php", fields).decode()
(target / "response.html").write_text(page, encoding="utf-8")
inputs = Inputs()
inputs.feed(page)
files = re.findall(r"one_file_download\('([^']+)','([^']+)','([^']+)','([^']+)'\)", page)
if not files:
    raise ValueError("No base files available")
records = []
for code, fn, kind, tmp in files:
    form = dict(inputs.fields)
    form.update(
        satellite_type="GRJE",
        data_type=kind,
        observation_date="2026-10-31",
        d_mode="[hour] GRJE 3.02 2026/10/31.05 2026/10/31.07",
        d_obs_cd=code,
        d_file=fn,
        d_tmp_dir=tmp,
        d_satellite="DOWNLOAD",
        d_rv="3.02",
    )
    content = get("https://terras.gsi.go.jp/download.php", form)
    (target / fn).write_bytes(content)
    if fn.endswith(".tar.gz"):
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tar:
            for m in tar.getmembers():
                if m.isfile():
                    (target / Path(m.name).name).write_bytes(tar.extractfile(m).read())
    else:
        (target / fn[:-3]).write_bytes(gzip.decompress(content))
    records.append({"file": fn, "request": form})
(target / "download.json").write_text(json.dumps(records, indent=2))
base = next(target.glob("*.26o"))
info = observations(base)
(target / "observation_info.json").write_text(json.dumps(info, indent=2))
nav = [x for x in target.glob("*.26*") if x.suffix.lower() in [".26n", ".26l", ".26q"]]
command = [
    str(p / "rtklib/rnx2rtkp.exe"),
    "-k",
    str(p / "ar_continuous.conf"),
    "-c",
    "-t",
    "-u",
    "-o",
    str(target / "ar_continuous.pos"),
    str(p / "rover.obs"),
    str(base),
    *[str(x) for x in nav],
]
(target / "command.json").write_text(json.dumps(command, indent=2))
with (target / "solve.log").open("w") as f:
    subprocess.run(command, stdout=f, stderr=f, check=True, timeout=180)
print(args.station, "done")
