# SignPath Foundation でコード署名する手順

Windows の Smart App Control(SAC) が未署名 exe をブロックするため、
配布 exe に **SignPath Foundation(OSS向け無料コード署名)** で署名する。

## 前提(このリポは全て満たしている)
- [x] OSI 承認ライセンス(MIT)
- [x] 公開リポジトリ(GitHub)
- [x] GitHub Releases で配布している
- [x] **CI で検証可能にビルド**(`.github/workflows/build.yml`。tag push で `GameServerManager.exe` をビルド・添付)

SignPath は「ローカルでこっそり作った exe」ではなく「CI で誰でも再現ビルドできる成果物」を署名対象にする。上記CIがそれ。

## あなたがやること(私は代行不可: reCAPTCHA + 本人確認があるため)

### 1. 申請
1. https://signpath.org/apply を開く
2. フォームに入力:
   - **Project name**: game-server-manager
   - **Repository URL**: https://github.com/Simohayhe/game-server-manager
   - **License**: MIT
   - **Description**: Hyper-V上のVMとゲームサーバー(Minecraft/ARK/Palworld)を管理するデスクトップアプリ
   - **CI**: GitHub Actions(`build` ワークフロー)
3. reCAPTCHA + 本人情報を入力して送信
4. 審査結果はフォームに登録したメールに届く(承認/却下)

> 注意: 新しい趣味プロジェクトは「verifiable reputation(実績)」が弱いと却下されることがある。
> その場合はしばらく運用してスター/リリース実績を積んでから再申請、が現実的。

### 2. 承認されたら(SignPath側でプロジェクト作成後)
SignPath の管理画面で以下の slug/ID が発行される:
- Organization ID
- Project slug(`game-server-manager`)
- Signing policy slug(例 `release-signing`)
- Artifact configuration slug(例 `exe`)

GitHub リポジトリ設定で登録:
- **Secrets** → `SIGNPATH_API_TOKEN`(SignPathで発行したAPIトークン)
- **Variables** → `SIGNPATH_ORG_ID`(Organization ID)

### 3. 署名を有効化
`.github/workflows/build.yml` の **`# SignPath 署名` ブロックのコメントを外す**
(slug が上と違えば合わせる)。以降 `git tag vX.Y.Z && git push --tags` で
**署名済み exe** がビルドされ、リリースに添付される。

## 承認までの暫定運用
署名が通るまでは、本番は python(pythonw main_service.py)で運用(SAC対象外)。
exe を試すときは SAC が弾く可能性があるので、その都度手動で許可するか python 運用のまま。
