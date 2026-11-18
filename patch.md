# Patch สำหรับรูปแบบคำตอบ JSON ของ TESA

แก้เฉพาะ `SYSTEM` ใน `strategy.py` โดยเพิ่มตัวอย่างคำตอบ อธิบายคีย์ และระบุให้ละตัวเลขที่ไม่มีในข้อมูล

## ใช้ patch ใน PowerShell

```powershell
git -C D:/tesa apply "C:/Users/GF/Documents/Codex/2026-11-18/g/outputs/tesa-json-response-format.patch"
```

## เนื้อหา patch

```diff
--- a/strategy.py
+++ b/strategy.py
@@ -935,8 +935,13 @@
 Aim for the round target within capacity and each candidate's max_bags. Use each candidate once at most.
 Keep candidate markup at zero urgency. At higher urgency favor sales and use only small discounts.
 OUTBID means competition; ABOVE_LIMIT means the buyer rejected the price.
-Return JSON matching the schema, with a brief reason and a short Thai note per listed buyer.
-Use only supplied facts. Omit unknown our_markup and winning_markup."""
+Return one JSON object matching this example shape. Use current input values.
+{"offers":[{"candidate":0,"bags":2,"markup":1.04}],"reason":"ใช้โควตากับร้านที่มีโอกาสขายสูง","buyers":[{"buyer":"Example buyer","situation":"ยังไม่เคยเสนอ","action":"คงราคา"}]}
+offers: proposed allocations ([] if none). candidate: integer ID copied from Candidates; bags: whole bag count;
+markup: price multiplier (1.04 means 104% of TV). reason: one short sentence about the overall plan.
+buyers: one note per listed buyer. buyer: exact name; situation: brief Thai status; action: brief Thai next step.
+Optional numbers: our_markup = our last markup; winning_markup = an explicitly reported competitor's winning markup.
+Use only supplied facts. Omit either optional number if unavailable."""

 SCHEMA = {"type": "object", "required": ["offers", "reason", "buyers"], "properties": {
     "offers": {"type": "array", "items": {"type": "object", "required": ["candidate", "bags", "markup"], "properties": {
```
