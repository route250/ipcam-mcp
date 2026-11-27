# WTW-IPW308TTW/BのLANサーバー

Ubuntu上のサーバーから、ブラウザーでの映像表示とパン・チルト操作、Streamable HTTPでのMCP接続を利用できるワン。
対象機の内部モデル識別子は `IPC-G3B2D0-TY` ワン。
通信はローカルLAN内で行い、クラウドAPIは使わないワン。

## カメラ用SDカード

純正のセットアップ用アクセスポイントを起動する状態のカメラと、WPA/WPA2-PSKで接続できるWi-Fiを用意するワン。
カメラとUbuntuを同じIPv4 LANに置き、ルーター側でカメラのインターネット通信を遮断するワン。
SD側のDHCP処理はIPv4アドレスとネットマスクだけを適用するワン。

以下の `SD` はUbuntu上のSDカードのマウント先に置き換えるワン。
既存の `update_script.sh` がある場合は、作業前にカードの内容をバックアップするワン。
初回の準備では、Ubuntuの `gcc-arm-none-eabi` に含まれるクロスコンパイラを使って起動用ヘルパーを作るワン。

```sh
SD=/media/"$USER"/IPCAM
arm-none-eabi-gcc -Os -marm -march=armv7-a -ffreestanding -static -nostdlib \
  -Wl,-e,_start,-Ttext=0x20000,-z,max-page-size=4096 \
  -o "$SD/ipcam-wifi-launch" sd-card/ipcam-wifi-launch.c
cp -i sd-card/*.sh "$SD/"
python3 sd-card/configure.py "$SD" --ssid 'リビングのSSID' --ssid '作業部屋のSSID' \
  --probe-ip 192.168.1.1 --probe-ip 192.168.1.10 --lan-cidr 192.168.1.0/24
sync
```

`--ssid` は接続先ごとに指定し、表示された順に各SSIDのパスフレーズを入力するワン。
コマンドは指定したSSID一式で `wpa_supplicant.conf` を作り直すワン。
カメラは登録先を同じ優先度で扱い、起動時に電波強度やセキュリティ方式に基づいて接続先を選ぶワン。
生成した `wpa_supplicant.conf` には、SSIDごとの `network={...}` と接続に使えるPSKが入るワン。
Telnetで設定を追加する場合は、SDの `/mnt/flash/mmc/wpa_supplicant.conf` に `network={...}` ブロックを追加するワン。

疎通確認先はSD直下の `lan-probe-ip` にIPv4アドレスを1行に1件書くワン。
`--probe-ip` で複数指定でき、各Wi-Fiからリスト内の少なくとも1件がpingに応答する必要があるワン。
DHCPの接続先を特定のIPv4ネットワークに制限する場合は、`--lan-cidr` でSD直下の `lan-cidr` を作るワン。
この制限を解除する場合は、`lan-cidr` を削除するワン。

既存カードから移行する場合は、使用中の `ipcam-telnet-local.sh` と、存在する場合は `ipcam-telnet-root.hash` を新しいSDカードの直下にコピーするワン。
`WifiInfo.ipcam-dummy` を使うカードでは、起動時にRAM上へコピーして一時適用するワン。
SDカードのマウント、純正の設定用AP、TuyaのLAN待ち受け（TCP 6668）を確認すると、SDのWi-Fi設定への切り替えに進むワン。
その状態で疎通確認に失敗すると、`ipcam-wifiinfo.disabled` を作り、純正APを待つ方式で再起動するワン。

SDの起動スクリプトはカメラのタイムゾーンを日本時間（`JST-9`、UTC+9）に設定するワン。
5秒間隔で `/tmp/TZ` を確認し、純正アプリが上書きした場合は設定し直すワン。

SDカードをカメラに挿して起動し、ルーターのDHCP割り当て一覧でカメラのIPを確認するワン。
以下は割り当てられたアドレスが `192.168.1.123` の例ワン。

```sh
curl --fail http://192.168.1.123:10081/device_model.txt
```

カメラのTCP 10081は、Wi-FiのIPv4アドレスで `device_model.txt`、`motorPosition`、`Version.txt`、`SysInfo` の4ファイルを配信するワン。
工場初期化直後など、カメラが位置を保存するまでは `/motorPosition` がHTTP 404を返すことがあるワン。
HTTP版は接続確認時の404に対し、短い上下移動を各1回試して再取得するため、起動前に可動範囲を空けるワン。
SDの起動処理が失敗した場合は、電源を切ってSDカードを取り外し、起動し直せるワン。

### 作業用のTelnetログイン

新しいSDカードに作業用のTelnetログインを設定する場合は、リポジトリにあるテンプレートと機器ごとのパスワードハッシュをSD直下に置くワン。
`openssl passwd -1` は対話入力でパスワードを受け取り、対象ファームウェアで動作を確認した形式のハッシュを出力するワン。

```sh
(umask 077; set -C; openssl passwd -1 > "$SD/ipcam-telnet-root.hash") &&
  cp -i sd-card/ipcam-telnet-local.sh.example "$SD/ipcam-telnet-local.sh"
```

このテンプレートは、カメラの起動中だけ既存Telnetサービスのroot認証を設定するワン。
元の認証に戻すには、SD直下の `ipcam-telnet-local.sh` を外してカメラを再起動するワン。

### カメラマイクの入力音量

SDカードの `update_script.sh` は、起動時にカメラの `/mnt/flash/data/hank_default_parameter.ini` の `[PARAMETER_ITEM]` に `InPutVolume=5` を保存するワン。
すでに同じ内容が保存されていれば、ファイルの書き込みを省略するワン。
以前の音声試験でSD直下に `ipcam-audio-local.sh` を置いたカードでは、再起動前にそのファイルを退避するワン。
再起動後は同じINIで `InPutVolume=5` を確認するワン。実際の入力ゲインは `/proc/umap/acodec` の `LGain(dB)` と `RGain(dB)` で確認できるワン。

## Ubuntuでの起動

Python 3.13、FFmpeg、curlを利用するワン。
設定例のIPv4アドレスとCIDRは自分のLANに合わせて変更するワン。

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
mkdir -p .config
cp ipcam-server.example.json .config/ipcam-server.json
.venv/bin/python -m ipcam_mcp --config .config/ipcam-server.json --port 9928
```

同じUbuntu上のブラウザーで `http://127.0.0.1:9928/` を開くワン。
MCPクライアントの接続先は `http://127.0.0.1:9928/mcp` ワン。
別PCから使う場合は、サーバーのLAN上のIPv4を `--host` に指定するワン。
ホスト名でアクセスする場合は `--allowed-host` も指定するワン。

動体検知と人検知の対象領域をすべて空にするには、`.config/ipcam-server.json` に `"disable_detection_regions": true` を追加してサーバーを起動するワン。
映像の接続・再接続前にLAN経由の `INF55` でカメラに空の領域を保存するワン。
設定を `false` にすると以降の上書きが止まり、領域を使うにはカメラ側で再設定するワン。

systemdのユーザーサービスを使う場合、同梱のユニットは `~/ipcam-mcp` に配置した場合の例ワン。
別の場所で使う場合は、ユニットの `WorkingDirectory` と `ExecStart` を変更するワン。

```sh
mkdir -p ~/.config/systemd/user
cp service/ipcam-server.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ipcam-server.service
```

## 原点復帰

操作画面の「原点復帰」を押すと、カメラが電源投入時と同じ動作を行い、パンとチルトの両軸が動くワン。
原点復帰の直後に右と左へ各0.1秒動かし、停止後のパンとチルトのステップ値を表示するワン。

## ステップと角度を指定して移動

操作画面で「ステップ」または「度」を選び、パンとチルトの目標を入力するワン。
片方を空欄にすると、その軸は開始時の位置を目標とするワン。
MCPの移動ツールは `camera_goto` で、目標を度で指定するワン。
角度指定の目標はパン・チルトともに範囲内の最も近い20°刻みに丸めるワン。例えば31°は40°、9°は0°になるワン。
角度指定では現在角度も同じ方式で丸め、丸めた目標と一致した軸は移動を省略するワン。
MCPで実測角度への復帰などを行う場合は `round_to_20_degrees: false` を指定できるワン。

サーバーは移動後のステップを読み、誤差が20ステップを超える軸を最大3回補正するワン。
同じ軸で方向を反転する指定移動では、パンは300 ms、チルトは390 msを指令時間に足すワン。
移動速度はソースの初期値を使い、方向パッドまたは指定移動の初回で同じ方向のずれが3回続いたときに更新するワン。
初回で許容差に入った移動と、指定移動の補正パルスは速度を変えないワン。
更新値は接続先IPごとにサーバーのメモリーで保持し、再起動時に初期値に戻るワン。

## 音声再生

`GET /audio` をSSEで開き、最初の `session` イベントの値を後続のPOSTの `X-Audio-Session` に指定するワン。
`POST /audio` に8 kHzのモノラルPCMを送ると再生IDが返り、開始、区切り到達、終了をSSEで受け取れるワン。
ヘッダーと一時停止・再開・キャンセルの呼び出し方はサーバー仕様に記載しているワン。[音声再生API](ipcam_mcp/SPEC.md#音声再生)

WAVファイルを再生するには、サーバーの起動後に次を実行するワン。

```sh
python3 tools/play_wav.py --server http://127.0.0.1:9928 speech.wav
```

CLIは最長60秒のWAVをFFmpegで変換し、サーバーの終了通知を待つワン。
Ctrl+CでCLIを終了するとSSE接続が切れ、サーバーが再生を中止するワン。

## テキストの読み上げ

Web画面の「読み上げ」にテキストを入力して「カメラで読み上げ」を押すワン。

同梱の `tts.sh` は日本語をOpen JTalkで合成し、FFmpegで8 kHz・モノラル・16 bit PCM WAVに変換するワン。
`OPEN_JTALK_VOICE` に音声モデルの絶対パスを指定できるワン。`OPEN_JTALK_PITCH` は半音単位の音程調整で、既定値は0、3半音下げる場合は `-3` ワン。
Ubuntuで同梱スクリプトを使うには、FFmpegに加えて次のパッケージを入れるワン。

```sh
sudo apt install open-jtalk open-jtalk-mecab-naist-jdic hts-voice-nitech-jp-atr503-m001
```

別のエンジンを使う場合は、サーバープロセスの環境変数 `IPCAM_TTS_COMMAND` に実行可能なスクリプトの絶対パスを設定するワン。
スクリプトは第1引数のテキストを読み上げ、第2引数のパスにWAVを出力するワン。
キャンセル時に子プロセスも停止するため、スクリプト内のコマンドは同じプロセスグループ内で実行するワン。

`GET /audio` のSSE接続を開いたまま、受け取ったセッショントークンを使ってテキストを送るワン。
次の例は再生位置0サンプルと800サンプル（100 ms）で通知を受け取るワン。

```sh
curl --fail http://127.0.0.1:9928/tts \
  -H "X-Audio-Session: $token" \
  -H 'Content-Type: text/plain; charset=utf-8' \
  -H 'X-Audio-Indexes: 0,800' \
  --data-binary 'カメラからのお知らせです。'
```

開始・区切り・終了の通知と一時停止・再開・キャンセルにはPCMと同じAPIを使うワン。
[音声再生API](ipcam_mcp/SPEC.md#音声再生)

## マイク録音

Web画面の「入力」でマイクを選び、「マイクを再生」で音を聴けるワン。
再生中に入力を選び直すと、その画面の再生だけが切り替わるワン。
`.config/ipcam-server.json` の `mic_source` は初期選択とCLIで使う入力を指定し、省略時は `"camera"` でカメラ内蔵マイクを使うワン。
Pi 4のUSBマイクを選択肢に追加するには、同じ設定ファイルに `usb_mic_device` を追加してサーバーを再起動するワン。

```json
{
  "usb_mic_device": "plughw:CARD=Device,DEV=0"
}
```

`usb_mic_device` はサーバー上の `arecord -L` で確認できるALSAのデバイス名ワン。
上の例は `USB PnP Sound Device` を選び、`plughw` が機器の録音レートから8 kHzに変換するワン。
USB入力には `arecord`（Debian系では `alsa-utils`）が必要ワン。
systemdのユーザーサービスで運用している場合は `systemctl --user restart ipcam-server` で設定を反映するワン。

サーバーで指定した既定のマイクを5秒間WAVに保存するには、サーバーの起動後に次を実行するワン。

```sh
.venv/bin/python tools/record_mic.py --server http://127.0.0.1:9928 --seconds 5 mic.wav
```

CLIは `/mic` のWebSocketを購読するワン。
WAVはカメラ入力なら元のサンプルレート（8 kHzまたは16 kHz）、USB入力なら8 kHzで、16 bit・モノラルとして保存するワン。
受信の途中で切断した場合は受信済みの音声をWAVに残し、エラーを返すワン。

既定のマイクにノイズ除去を使う場合は、無音時のマイクを配信と同じサンプルレートで録音した16 bit・モノラルのWAVを、サーバー設定ファイルと同じディレクトリの `mic-noise-profile.wav` に置いてからサーバーを起動するワン。
別の場所に置く場合は、サーバー起動時に `--mic-noise-profile` で指定できるワン。
CLIの `--denoise` はノイズ除去あり、`--no-denoise` と指定省略時はノイズ除去なしワン。

```sh
.venv/bin/python tools/record_mic.py --denoise --seconds 5 filtered.wav
```

## 自動テスト

サーバーの依存パッケージをインストールした環境で実行するワン。

```sh
.venv/bin/python -m unittest discover -s tests -v
```

[サーバー仕様](ipcam_mcp/SPEC.md)
