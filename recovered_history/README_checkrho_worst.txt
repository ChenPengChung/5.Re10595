最大質量震盪 checkrho.dat — 還原紀錄
=====================================
來源分支 : origin/Edit2_varyGamma  (varyGamma 網格拉伸實驗)
blob     : b9665285b64833a326c24239a965494ee19a74bf  (checkrho.dat.gz, 最完整版 326,680 行)
          c5620bd2e8588d9edf1bbcaf77a2bf36938b68b8  (同一次 run 的未壓縮 checkrho.dat, 325,446 行)
首次出現 commit:
  c334ab2  2026-05-13 20:18  更新: FTT_STATS_START 80→100 ... (raw .dat)
  0627264  2026-05-13 20:25  三大紀錄檔改為壓縮追蹤 (.dat.gz)
  43d84b8  2026-05-14 17:33  (gz 最新快照)

特性 (這就是「質量修正打開前」的基準):
  - Col7 SKIP_MC=1 (質量修正關閉) 區段: FTT 0 → ~64, rho_avg 單調漂移 1.0 → 0.9985373 (-1.4627e-3)
    全域最劇: max|drift| = 1.462668e-03 @ FTT=64.097, step=27078301, rho_min=0.9985373318
  - FTT ~64→66 SKIP_MC 由 1→0 (打開質量修正): rho_avg 立刻回到 1.0000000, drift ~1e-9, 維持到 FTT 77
  - 全 git 歷史 52 個 checkrho blob 中震盪最大者 (次大僅 2.9e-6, 相差 ~500x)

欄位: Col1 step | Col2 FTT | Col3 rho_target(=1) | Col4 rho_avg | Col5 rho_drift(=rho_avg-1)
      | Col6 rho_correction | Col7 SKIP_MC (0=修正ON, 1=修正OFF)

與「插值→checkpoint→Jacobian」突破的關係:
  此檔為 5/19 體積加權 (Σ(ρ·V)/Σ(V)) 與後續 Jacobian Gauss-Legendre 體積修正「之前」的
  未修正質量漂移實證。突破 commit: 7b817f2 / 65e1c08 (2026-05-19 早上)。
