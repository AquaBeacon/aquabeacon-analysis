# 校正の再利用・数値再計算用データ

元の `data/raw/` は変更せず、既存の解析出力から2026-10-31にコピーした約12 MBの根拠データ。
SHA-256、コピー元、サイズは [manifest.json](manifest.json) に保存する。
このフォルダと `calibration/*.json` は改行変換を無効にし、別PCでも同じバイト列を保持する。

| 保存先 | 内容と座標・単位 |
|---|---|
| `camera/observations.npz` | MP4の43検出。`times`は動画秒、`corners`は1920×1080座標の20交点。検出時の縮小画像から原解像度へ換算済み |
| `camera/proxy_selected_observations.npz` | LRFの採用35検出、1280×720座標。`group`・前半の選択マスクと残差も保持 |
| `camera/proxy_observations.npz` / `proxy_early_observations.npz` | 後半22枚と前半20枚の検出候補。未採用・旧候補も区別して保存 |
| `camera/contact_sheet.jpg` | 元動画の確認用縮小画像 |
| `child/pose_1_imu.csv`〜`pose_4_imu.csv` | 子機003の4姿勢区間。加速度m/s²・角速度rad/s、`acquired_us`は単調時計µs。`fresh_mask`を保持 |
| `parent/pose_1_imu.csv`〜`pose_4_imu.csv` | 親機011の4姿勢区間。`corrected_a*`はm/s²、`corrected_g*`はrad/s、`parent_mono_s`は親機単調時計秒 |
| `child/axis_audit/`、各`report.md`・`manifest.json`・JPG | 元解析の監査、来歴、適用条件、確認画像 |

CSVは元出力のまま保持する。軸再計算では校正JSONの各姿勢開始から1秒、終了前0.25秒を除き、
子機は加速度fresh bit 2、角速度fresh bit 4の行をそれぞれ選ぶ。親機は`corrected_*`列を使う。
NPZは`numpy.load(..., allow_pickle=False)`で読める数値配列のみ。

```powershell
python scripts/verify_calibration_bundle.py
python scripts/verify_calibration_bundle.py --refit
```

`--refit`はMP4の採用34枚、LRFの35枚から4×5交点・40 mm間隔・`CALIB_FIX_K3`で
カメラを再推定し、保存値との内部行列差0.01 px以下・歪み係数差1e-4以下を確認する。
親子の軸変換も再計算し、保存行列との差1e-8以下を確認する。既存校正値は上書きしない。

カメラの交点検出そのものをやり直すには原動画が必要。全動画、全収録のIMU、生成地図、
Blender成果物は同梱しない。コピーした監査文書内の相対リンクは元の結果フォルダ基準のため、
同梱されない動画等へのリンクも残る。原動画ハッシュと元パスは来歴として保持する。

水中内部校正は同一カメラ・ケース・撮影条件でのみ再利用し、取付回転は同じ実装状態でのみ使う。
校正動画の時計合わせは新しい実験へ転用しない。磁気方位の検証不合格・暫定取付け・
未校正の光学extrinsicも元の状態を保持する。
