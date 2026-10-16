# C_MAIN・P_MAIN ToF 音速補正の根拠と条件

## 対象と目的

`data/processed/20260922_cmain_{022,023,024}/tof_window/c_main/C_MAIN_HEALTH_RANGE_*.csv` の
`tof_us` を音響距離に換算し、同時刻の `pressure_mbar` から求める深度と比較する。
P_MAINの独立したToFログも、C_MAINと同じ `shot_seq` かつUTC差1秒以内の測定に限って比較する。
この条件に一致するのはC_MAIN 022とP_MAIN 024であり、ログ番号だけでは対応付けない。
各実験の結果は `results/<experiment_id>/tof_depth_comparison/` に保存する。
元CSVおよび `data/raw/` は変更しない。

## 音速式

Medwin (1975) の近似式を各行に適用する。

```text
c(T,S,z) = 1449.2 + 4.6T - 0.055T² + 0.00029T³
           + (1.34 - 0.010T)(S - 35) + 0.016z
r = c(T,S,z) × tof_us × 10⁻⁶
```

- `c`: 音速 [m/s]
- `T`: C_MAIN HEALTH_RANGE の `temp_c` [°C]
- `S`: 塩分 [‰]。測定列がないため **35‰を仮定**する。
- `z`: 同じ行の圧力換算深度 [m]
- `r`: 親機との音響斜距離 [m]。`tof_us` を**片道伝搬時間**と仮定する。
- P_MAINの `temp_c` と `pressure_mbar` はNaNのため、対応するC_MAIN行の `T` と `z` で
  P_MAINのToFも換算する。両機のToFは同じ音路を観測すると仮定する。
- 適用範囲: 0–35°C、0–45‰、0–1000 m。今回の入力は範囲内。

資料: Herman Medwin, “Speed of sound in water: A simple equation for realistic parameters,”
*The Journal of the Acoustical Society of America* 58(6), 1318–1319 (1975),
[DOI:10.1121/1.380790](https://doi.org/10.1121/1.380790)。
[著者所属機関の公開PDF](https://calhoun.nps.edu/server/api/core/bitstreams/35d133d5-6956-4912-8597-5f2dc8fc5ad2/content)、
[MIT OpenCourseWare の式の記載](https://ocw.mit.edu/courses/2-011-introduction-to-ocean-science-and-engineering-spring-2006/073d1246f6aac0102c6b29e0bfdfa5bc_hw5_sonar_leonar.pdf)。

## 圧力から深度への換算

前回の比較と同条件として、次を使用する。

```text
z = (pressure_mbar - 1013.25) × 100 / (1025 × 9.80665)
```

水面気圧 1013.25 mbar、海水密度 1025 kg/m³ は測定値ではなく仮定である。
現地の水面気圧と塩分が得られたら置き換える。

## 解釈上の制約

- `temp_c` が周囲水温を測っているか、センサ内部温度かはログのみでは確定できない。
  `rtc_temp_c` は別の温度列なので音速計算に使わない。
- 水路全体の温度・塩分分布は測定されていない。子機位置の `temp_c` を音路の代表値とする近似である。
- `tof_us` の電子回路遅延と送受信固定遅延は未補正。
- P_MAINの記録時刻は対応するC_MAIN行と最大約0.66秒異なる。対応にはUTCだけでなく
  `shot_seq` を必須とし、1秒を超える組は採用しない。
- C_MAIN 023・024に対応するP_MAIN ToFログは今回の `data/processed/` にない。
- ToFは親子機間の斜距離、圧力は水面基準の子機深度であり、両者の差は真値誤差ではない。
- `tof_valid=1` は既存の `tof_us > 0` かつ `range_state ∈ {2,3}` を意味する。
  この判定を通過した単発の大きなToF値も図と集計に残す。

## 再実行と出力

```powershell
python scripts/compare_tof_depth.py
```

既定値は [`config/tof_depth_comparison_20260922.json`](../config/tof_depth_comparison_20260922.json)
に記録する。必要なら `--salinity-ppt`、`--surface-pressure-mbar`、`--density-kg-m3` を変更する。
各実験の `paired_observations.csv` は元の全行を保持し、無効ToFの距離列は空欄にする。
P_MAINに一致する行にはToF、時刻差、補正距離を追加する。
`summary.json` は仮定、C_MAIN/P_MAIN入力SHA-256、config SHA-256、Git commit SHA、
集計値を記録し、未使用のcalibrationは `null` と明示する。
`comparison.png` は時系列と散布図を示す。022はP_MAINとC_MAINの直接比較も含む。
`pressure_vs_p_main.png` は深度とP_MAIN ToF距離のみを表示し、凡例を `Depth` と
`ToF range` とする。P_MAINがない023・024には生成しない。

## 検証記録（2026-09-23）

- `python -m pytest -q -p no:cacheprovider`: 63件成功。
- `python -m ruff check .`: 成功。
- 022、023、024の `comparison.png` を目視確認。022はC_MAIN/P_MAINの重ね描きと
  対応shotの散布図を確認。023・024にはP_MAINを捏造せず、深度線、C_MAIN ToF点、
  単発の大きな値を表示している。無効ToF区間を点間の線で補間していない。
- 022の `pressure_vs_p_main.png` を目視確認。C_MAIN深度線とP_MAIN ToF点だけを表示し、
  凡例が `Depth` と `ToF range` であることを確認した。
