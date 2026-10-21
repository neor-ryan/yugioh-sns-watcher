"""YouTube Analytics（視聴者維持率）を読むための許可を、一度だけブラウザで行う。

  pip install google-auth-oauthlib   # このスクリプトでだけ使う
  python tools/google_auth.py

ブラウザが開くので、チャンネルを持っている Google アカウントで「許可」を押す。
結果は google_token.json（.gitignore 済み）に保存する。中の refresh_token・client_id・client_secret を
GitHub の Secrets（YOUTUBE_REFRESH_TOKEN / YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET）に登録して使う。
許可を取り消したときや、トークンが無効になったときは、もう一度これを実行する。
"""
import json
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

ROOT = Path(__file__).resolve().parent.parent
SCOPES = ["https://www.googleapis.com/auth/yt-analytics.readonly"]

flow = InstalledAppFlow.from_client_secrets_file(str(ROOT / "client_secret.json"), scopes=SCOPES)
creds = flow.run_local_server(
    port=0,
    access_type="offline",
    prompt="consent",  # 毎回 refresh_token を発行させる
    success_message="許可できました。このタブは閉じて大丈夫です。",
)
if not creds.refresh_token:
    raise SystemExit("refresh_token が取得できませんでした。もう一度実行してください。")

out = ROOT / "google_token.json"
out.write_text(
    json.dumps(
        {"client_id": creds.client_id, "client_secret": creds.client_secret, "refresh_token": creds.refresh_token},
        indent=2,
    ),
    encoding="utf-8",
)
print(f"保存しました: {out}")
