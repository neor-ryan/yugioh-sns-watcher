/**
 * GitHub Actions の定時実行は数時間遅れたり飛ばされたりするため、
 * Google Apps Script（1分ごとのトリガー）から workflow_dispatch で起動する。
 *
 * 設定（script.google.com）:
 *   1. このファイルの中身を新しいプロジェクトに貼る
 *   2. プロジェクトの設定 → スクリプト プロパティ に GITHUB_TOKEN（Actions: Read and write の fine-grained token）を追加
 *   3. testSweep を実行して「204」が出ることを確認（初回は権限の許可を求められる）
 *   4. setup を1回実行（1分ごとのトリガーができる）
 */
const REPO = 'neor-ryan/yugioh-sns-watcher';

function tick() {
  const now = new Date();
  const ymd = Utilities.formatDate(now, 'Asia/Tokyo', 'yyyy-MM-dd');
  const hh = Number(Utilities.formatDate(now, 'Asia/Tokyo', 'H'));
  const mm = Number(Utilities.formatDate(now, 'Asia/Tokyo', 'm'));

  // 集中監視：11:50 / 20:50。トリガーが1分飛んでも拾えるよう :54 までに1回だけ送る
  if ((hh === 11 || hh === 20) && mm >= 50 && mm <= 54) {
    dispatchOnce_('window.yml', 'window-' + ymd + '-' + hh);
  }
  // 通常監視：毎時17分（:21 までに1回）。集中監視の時間帯はスクリプト側でスキップされる
  if (mm >= 17 && mm <= 21) {
    dispatchOnce_('sweep.yml', 'sweep-' + ymd + '-' + hh);
  }
}

function dispatchOnce_(workflow, key) {
  const props = PropertiesService.getScriptProperties();
  if (props.getProperty('last_' + workflow) === key) return;
  if (dispatch_(workflow) === 204) props.setProperty('last_' + workflow, key);
}

function dispatch_(workflow) {
  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) throw new Error('スクリプト プロパティに GITHUB_TOKEN が設定されていません');
  const res = UrlFetchApp.fetch(
    'https://api.github.com/repos/' + REPO + '/actions/workflows/' + workflow + '/dispatches',
    {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify({ ref: 'main' }),
      headers: {
        Authorization: 'Bearer ' + token,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
      },
      muteHttpExceptions: true,
    }
  );
  const code = res.getResponseCode();
  if (code === 204) console.log(workflow + ' を起動しました');
  else console.error(workflow + ' の起動に失敗: ' + code + ' ' + res.getContentText().slice(0, 300));
  return code;
}

/** 1分ごとのトリガーを作る（何度実行しても1つだけになる） */
function setup() {
  ScriptApp.getProjectTriggers()
    .filter(function (t) { return t.getHandlerFunction() === 'tick'; })
    .forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('tick').timeBased().everyMinutes(1).create();
  console.log('1分ごとのトリガーを作りました');
}

/** 動作テスト：通常監視を今すぐ1回起動する。204 なら成功 */
function testSweep() {
  console.log('結果: ' + dispatch_('sweep.yml') + '（204 なら成功）');
}
