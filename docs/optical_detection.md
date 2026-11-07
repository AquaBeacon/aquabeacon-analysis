# プール・南予の光源検出改善

プールの追加評価：[親機フレーム・位置フォールバック・マッピング](../results/20261028_ngt_000/optical_followup/pool_multiscale_20261104/report.html)。
002〜004の原LRFを同じフレームで比較し、旧有効点・既存除外を保持して、画像同定した55点を追加した。
既存同定除外後の光源候補成立率62.4→64.7%、位置フォールバック成立率67.7→70.9%。
静止002のRMSは変わらず、追加マッピングの実寸採用モデルはなかった。元の位置・地図は残している。

入口：[光源候補と位置成立率の比較](../results/20261103_field_review_000/optical_detection/multiscale_v2/report.html)。
追加：[親機フレーム付きの原因分析・位置評価・マッピング](../results/20261103_field_review_000/optical_followup/optical_fallback_20261104/report.html)。
処理仕様と受け入れ条件は[spec.md](spec.md)の「南予の光源検出回復」を参照。

従来の青緑色ハロー／白飛び核だけでは、白色・赤紫色・弱い光源の初期検出が失敗していた。
色に依存しない局所輝度と形状を優先し、弱い核は青緑色の検出でも確認する。背景の変化の影響を減らすため固定テンプレートを
81 pxから41 pxへ変更した。強い光源核と前後画像の移動が一致する場合は小さな固定核でも検査する。
短い欠測は最後の実測点の近傍に限定し、競合候補を拒否する。1秒相当の失敗後は確認点での再初期化を待つ。
既存の目視同定点を共有し、保存した確認画像そのもので初期化する。別時刻の画像へ座標を直接適用しない。

このPCの入力は移行済み高解像度MP4。LRF原本を使った過去結果との差を隠さず、
同じ検証用動画上で旧方式も再実行する。光源が同定できない区間は欠測のまま残る。
旧有効点を保持して新規候補を追加した系列と、新旧アルゴリズム単体の成績を分けて報告する。

## 再現

依存は`.[analysis,media,vision,dev]`。GPU proxy生成はCUDA/NVDEC/NVENC対応のFFmpegとNVIDIA GPUが必要。
GPU処理は同時に1つ、CPUデコード2スレッド・フィルタ1スレッド、proxyごとの上限900秒。
検出処理はCPUで最大2区間並列、各デコード2スレッド・画像処理1スレッド。全体の時間上限は設定JSONに保存する。

```powershell
$trialArgs = @('--experiment','20261031_nanyo_001','--experiment','20261031_nanyo_002',
  '--experiment','20261101_nanyo_001','--experiment','20261101_nanyo_002',
  '--experiment','20261101_nanyo_003','--experiment','20261101_nanyo_004',
  '--experiment','20261101_nanyo_005')
python scripts/prepare_nanyo_detection_proxy.py @trialArgs
python scripts/improve_nanyo_light.py @trialArgs --run multiscale_v2
python scripts/audit_nanyo_detection_inputs.py @trialArgs
python scripts/review_nanyo_light_recovery.py @trialArgs --phase prepare
python scripts/review_nanyo_light_recovery.py @trialArgs --phase apply
python scripts/evaluate_nanyo_light_recovery.py @trialArgs --run multiscale_v2
python scripts/verify_nanyo_light_recovery.py @trialArgs
python -m pytest tests/test_beacon_detection.py tests/test_beacon_tracker.py tests/test_beacon_halo.py tests/test_nanyo_light_recovery.py -q -p no:cacheprovider
```

生成済みproxyは再利用し、最初のコマンドは省略する。完成runがある場合は別の`--run`を指定する。
短い試行は`--max-frames 2000 --run pilot_name`。部分試行を全区間の位置評価に渡すとエラーになる。
新しい取得実験には今回のレビュー点・時計・固定ID一覧を流用しない。
レビュー適用は`config/20261103_field_review_000/optical_detection_identity_review.json`に保存した
今回のrunとCSVハッシュに限る。別runやコード変更後は確認画像を再検査し、別のレビュー設定を
`--review-config <path>`で指定する。除外番号の暗黙流用を拒否する。

## 出力

`data/interim/<id>/optical_detection/parent_30hz.mp4`は再生成可能な検証用動画。
同名JSONに元MP4・処理コマンド・PTS規約・ハッシュ・プロセス終了を保存する。
原本や移行済みMP4を上書きしない。

各実験の`results/<id>/optical_detection/<run>/`に次を保存する。

- `camera_relative_candidates.csv`：旧有効点と追加点。`recovered_candidate`、実画像UTC、元動画秒、検出状態を併読する。
- `manifest.json`：検出設定、実験ID、入力ハッシュ、Git SHA、時刻一致の最大差。
- `recovered_review_*.jpg`と`review_samples.json`：追加点を10秒以上離して選んだ画像、対応行・時刻。
- `boundary_review_*.jpg`と`boundary_review_samples.json`：10行を超える間隔で分けた追加区間の最初と最後。
- `manual_review.json`・`identity_review_manifest.json`：確認画像・理由・明示除外・入力と出力のハッシュ。
- `reviewed_camera_relative_candidates.csv`：画像確認の除外を適用した位置評価用入力。除外前のCSVを保持する。
- `positions_10hz.csv`・`positions_at_acoustic_events.csv`：同じ既存幾何での追加後の位置候補。
- `stationary_rms.csv`：固定の静止窓における平均周りのRMS。独立した真値誤差ではない。
- `evaluation.json`・`report.html`：旧値保全・除外・時間列の検証と位置比較。

集約結果の`verification.json`に入力・レビューのハッシュ、欠測、時刻、位置の有効フラグ、
入力MP4のサイズ・更新時刻、GPU処理完了、レポートリンクの検証を記録する。

初期化画像と再処理入力の照合は各実験の`optical_detection/input_audit/`に保存する。
LRFとMP4で画素が同じ瞬間を表すとは限らないため、画像比較の時間差と位置差を併記する。
位置計算は旧点にLRF用校正を保持し、追加点にMP4用校正を使う。追加点の座標は画素中心規約で
元の1920×1080へ戻す。`optical_intrinsics_source_kind`と`evaluation.json`から適用内容を確認できる。

候補数の増加だけでは全フレームの光源同定の正しさを保証しない。
取りこぼし・遮蔽・画角外・同定除外を区別し、追加点の画像監査とともに利用する。

## 今回の評価結果

南予7区間・39,741画像、同じ位置格子39,740点で比較した。
画像282枚を確認し、自動追加5,048点のうち誤追跡または同定不明の211点を除外、4,837点を追加した。

| 指標 | 旧系列 | 追加・除外後 |
| --- | --- | --- |
| 光源候補の成立率 | 23.1% | 35.3% |
| 光学＋姿勢＋深度の位置成立率 | 21.4% | 33.3% |
| 位置フォールバック全体の成立率 | 26.3% | 37.3% |

上記は旧有効点を保持した補完結果。新旧方式を同じMP4へ単独適用すると、候補成立率は
旧22.1%・新28.9%（追加分の同定除外前）であり、区間によって新方式が下回る。
網やロープの反射を完全には排除できず、未レビューの新規動画への完全自動適用は未検証。
真値に対する検出recall・precision、絶対位置精度の改善はこの評価だけでは断定しない。

## 原因フレームとマッピングの追加評価

11月1日の5区間を含む親機全画面30例に、実時刻・状態・目視説明を付けた。
画角外や微小光源に加えて、明るい光源でも再初期化待ち・既存初期化設定の空白で欠測する例がある。
静止窓では平均周りRMSが小さくなる例があるが、意図的な揺動区間では追加後のRMSが大きくなる。

上記の`$trialArgs`を用いる。既存画像・特徴点を再利用するため、追加のGPU動画変換は不要。

```powershell
python scripts/analyze_nanyo_optical_followup.py @trialArgs
python scripts/map_nanyo_optical_followup.py @trialArgs
python scripts/write_nanyo_optical_followup.py @trialArgs --render
python scripts/verify_nanyo_optical_followup.py @trialArgs
python -m pytest tests/test_nanyo_followup.py -q -p no:cacheprovider
```

数値設定とrun名は`config/20261103_field_review_000/optical_followup.json`、確認した親機画像の説明は
`optical_followup_frame_review.json`。マッピングの完成／途中runの上書きを拒否するため、別試行は別runとする。
各実験の`optical_followup/<run>/`に新旧位置、共通時刻の静止RMS、光学・音響整合性、親機全画面、
子機の確認画像、旧新prior、COLMAP結果、PLY、PNG/SVG、ソース・入力manifestを保存する。
マッピングは各区間の明示した範囲に最大300画像を使った探索的な比較で、全動画の再構成率ではない。
暫定的に実寸配置できた断片と、負尺度や対応不足の局所点群を区別する。既存の統合点群へは混ぜない。

## プールでの再現

設定は`pool_optical_followup.json`、採否は`pool_optical_identity_review.json`、原因写真の説明は
`pool_optical_frame_review.json`（すべて`config/20261103_field_review_000/`）。
同一LRF・同一時刻の旧検出を再現し、多尺度候補を全件確認してから追加する。
光源の画角外・遮蔽は補わず、全RGBの白色飽和を満たさない可視の発光面を回復する。
機体表面・プール底・ロープ・皮膚の反射候補は不採用にする。原本は変更しない。

```powershell
$poolArgs = @('--experiment','20261028_ngt_002','--experiment','20261028_ngt_003','--experiment','20261028_ngt_004')
python scripts/improve_ngt_light.py @poolArgs --phase detect
python scripts/review_pool_optical_frames.py @poolArgs
# 出力CSVハッシュと全提案画像に対応する採用・不採用行をレビュー設定に記録する
python scripts/improve_ngt_light.py @poolArgs --phase apply
python scripts/analyze_pool_optical_followup.py @poolArgs
python scripts/map_pool_optical_followup.py @poolArgs
python scripts/prepare_pool_dense_followup.py --experiment 20261028_ngt_004
python scripts/map_pool_optical_followup.py --experiment 20261028_ngt_004 --config config/20261103_field_review_000/pool_mapping_dense.json
python scripts/write_pool_optical_followup.py @poolArgs --render
python scripts/verify_pool_optical_followup.py @poolArgs
python -m pytest tests/test_pool_optical_followup.py -q -p no:cacheprovider
```

完成・途中runは保護する。別試行は設定のrunを明示的に変え、異なるCSVに同定行番号を流用しない。
集約レポートと検証は002〜004の全IDを明示する。既存画像の1/2 Hz試行と、004後半の10 Hz試行を区別する。
マッピングは同じ子機画像・特徴点・内部校正・乱数seedで旧／新の位置priorを比較する。
親機LRFの90°と子機MP4の180°をそれぞれ検証し、子機画像は画素中心規約を保った1280×720を使う。
符号・尺度の確定や絶対精度を保証しない。003の水上像に水中内部校正を適用する制約、
ぼけ・特徴不足・位置対応不足を写真とモデルごとの指標で残す。

## プール004：画像対応と位置拘束の切り分け

[原因診断レポート](../results/20261028_ngt_004/mapping_diagnosis/wall_ablation_v2_20261104/report.html)では、
壁が見える1505〜1535秒の同じ300画像を、無拘束・旧位置・新位置・旧位置の再試行で比較する。
既存DBの画像・特徴点・対応・内部校正を使い、位置拘束だけを変える。
旧新の252点と48欠測は完全一致し、今回の壁区間では光源検出の追加効果が入力にないことを確認した。
無拘束でも12画像・7点に留まり、主な制限は対応の継続と3D幾何にある。
残った小さな点数差は、旧位置の再試行でも生じるため改善効果に数えない。

```powershell
python scripts/diagnose_pool_mapping.py --experiment 20261028_ngt_004 --phase run
python scripts/diagnose_pool_mapping.py --experiment 20261028_ngt_004 --phase report
python scripts/diagnose_pool_mapping.py --experiment 20261028_ngt_004 --phase verify
python -m pytest tests/test_mapping_diagnostics.py -q -p no:cacheprovider
```

設定は`pool_mapping_diagnosis.json`。数値runは上書きせず、全条件を別runで比較する。
対応の時刻分断は、最大10画像先までのペアが時刻境界をまたぐかで判定する。
この設定での分断を元動画の全フレームや他方式の追跡限界へ一般化しない。
2D図はPNG/SVG/PDFと編集用元図、全ペアと画像の診断はCSV、点群は局所座標で保存する。
