#!/usr/bin/env bash
# 本轮用法端到端示例: 评审资格停用/启用
#   录入 -> 发布 -> 确认 -> 会务方凭密钥+修订号+非空原因停用评审人 ->
#   现有凭据立即 403 (取稿/决定/评语) -> 补位释放其已确认槽位 ->
#   同状态同原因幂等 / 同状态异原因 409 / 过期修订号 409 ->
#   重新启用只恢复当前发布版仍分配给本人的任务 -> 查看变更记录
# 前置: 建议对新库运行 (脚本会自行建数据; 若编号已存在可忽略 409)。
# 用法: ./examples/status.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }

echo "== 1. 录入 4 名不同机构评审人 + 1 篇论文 =="
for spec in 'R1 r1-secret Inst-A' 'R2 r2-secret Inst-B' 'R3 r3-secret Inst-C' 'R4 r4-secret Inst-D'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 3, \"avoid_papers\": []}"
done
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"],
  "institutions": ["Univ-X"]}'
# 既有评审人初始视为启用
curl -s "$BASE/reviewers" -H "$ORG" | j 'd["reviewers"]'

echo "== 2. 预演 + 发布 =="
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
PUB=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}")
SER=$(echo "$PUB" | j 'd["serial"]')
RA=$(echo "$PUB" | j 'd["plan"]["P1"][0]')
RB=$(echo "$PUB" | j 'd["plan"]["P1"][1]')
CA="r$(echo "$RA" | sed 's/R//')-secret"
CB="r$(echo "$RB" | sed 's/R//')-secret"
echo "P1 (serial=$SER) 分配: $RA, $RB"

echo "== 3. 两名评审人均确认 (形成已确认槽位) =="
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'

echo "== 4. 会务方停用 $RB (携带当前修订号与非空原因) =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
DIS=$(curl -s -X PUT "$BASE/reviewers/$RB/status" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"status\": \"disabled\", \"reason\": \"调查期间暂停评审资格\"}")
echo "$DIS"
REV=$(echo "$DIS" | j 'd["revision"]')

echo "== 4b. 匹配修订号下重复提交同一状态+同一原因 -> changed=false, 不推进修订号 =="
curl -s -X PUT "$BASE/reviewers/$RB/status" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"status\": \"disabled\", \"reason\": \"调查期间暂停评审资格\"}"; echo

echo "== 4c. 同状态但原因不同 -> 409 拒绝, 状态与记录均不变 =="
curl -s -w '\n-> HTTP %{http_code}\n' -X PUT "$BASE/reviewers/$RB/status" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"status\": \"disabled\", \"reason\": \"另一个原因\"}"

echo "== 4d. 过期修订号 -> 409 (先核对修订号; 未知评审人+过期修订号同样 409) =="
curl -s -w '\n-> HTTP %{http_code}\n' -X PUT "$BASE/reviewers/$RB/status" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"base_revision\": 0, \"status\": \"enabled\", \"reason\": \"过期请求\"}"

echo "== 5. 停用立即生效: $RB 现有凭据不能取稿/提交决定或评语 (凭据仍识别 -> 403) =="
curl -s -w '\n-> HTTP %{http_code}\n' "$BASE/reviewer/assignments" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB"
curl -s -w '\n-> HTTP %{http_code}\n' -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'
curl -s -w '\n-> HTTP %{http_code}\n' -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"score\": 3, \"comment\": \"停用后不应收到\"}"
# 凭据错误仍是 401; 另一评审人不受影响
curl -s -w '\n-> HTTP %{http_code} (错误凭据)\n' "$BASE/reviewer/assignments" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: wrong"
echo "$RA 仍可取稿:"
curl -s "$BASE/reviewer/assignments" -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  | j '[p["paper_id"] for p in d["assignments"]]'

echo "== 6. 既有发布分配与确认不删除 (会务方视图仍可见, 槽位确认状态保留) =="
curl -s "$BASE/assignment" -H "$ORG" | j 'd["plan"]'
echo "停用者清单:"
curl -s "$BASE/meta" -H "$ORG" | j 'd["disabled_reviewers"]'

echo "== 7. 补位预演: $RB 的已确认槽位释放 (fixed_problems 标 reviewer_disabled),"
echo "          $RA 的已确认槽位保持固定 =="
DRY=$(curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG")
echo "$DRY" | j '{"plan": d["plan"], "fixed": d["fixed"], "fixed_problems": d["fixed_problems"]}'
BREV=$(echo "$DRY" | j 'd["revision"]')
BSER=$(echo "$DRY" | j 'd["serial"]')
echo "== 7b. 补位发布 (双版本复核) =="
BP=$(curl -s -X POST "$BASE/assignment/backfill/publish" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $BREV, \"base_serial\": $BSER}")
echo "$BP" | j '{"serial": d["serial"], "plan": d["plan"], "carried_confirmations": d["carried_confirmations"]}'
RC=$(echo "$BP" | j 'next(x for x in d["plan"]["P1"] if x != "'$RA'")')

echo "== 8. 重新启用 $RB: 有效变更推进修订号 (启用原因同样必须非空) =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X PUT "$BASE/reviewers/$RB/status" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"status\": \"enabled\", \"reason\": \"调查结束, 恢复评审资格\"}"; echo

echo "== 9. 启用后不找回已被补位替换的槽位: 当前发布版中 $RB 无任务 =="
curl -s "$BASE/reviewer/assignments" -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  | j '{"serial": d["serial"], "assignments": [p["paper_id"] for p in d["assignments"]]}'
echo "补位者 $RC 正常取稿, $RA 的确认槽位保留:"
curl -s "$BASE/reviewer/assignments" -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  | j '{"assignments": [p["paper_id"] for p in d["assignments"]], "states": d["states"]}'

echo "== 10. 查看变更记录 (按人 / 全局) =="
echo "-- $RB 的资格历史:"
curl -s "$BASE/reviewers/$RB/status" -H "$ORG" | j 'd'
echo "-- 全部评审资格变更记录:"
curl -s "$BASE/reviewer-status-changes" -H "$ORG" | j 'd["changes"]'
