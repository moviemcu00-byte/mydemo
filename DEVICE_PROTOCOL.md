# Device Protocol — non-Android agents (Windows/Linux/macOS)

Feature #59 (Cross-device). Ye backend abhi **sirf Android phone** ke liye poora
tested hai (`app/` folder). Windows/Linux/macOS "agent" ka koi code is project
mein nahi hai — ye document bata deta hai ki koi bhi aisa agent kaise likha
jaaye ki backend use turant pehchan le. Aage koi bhi language mein likha ja
sakta hai (Python, Node, C#, Go...), jab tak protocol follow ho.

## 1. Connect
```
WebSocket -> wss://<backend>/device
Header:      Authorization: Bearer <apna token — Railway DEVICES variable mein>
```
Railway variable: `DEVICES=laptop:windows:<40+ random chars>,...`
(id: chhota naam jo tum khud choose karo. platform: `windows` | `linux` | `macos`.)

## 2. Apni capabilities batao (connect hote hi, ek baar)
```json
{"type": "hello", "capabilities": ["system_info", "shutdown"]}
```
- Naam: `a-z` se shuru, sirf `a-z 0-9 _`, 2-32 characters.
- Jo yahan nahi bataoge, wo Claude use kar hi nahi payega (`device_call` reject karega) —
  isse tumhara agent sirf wahi expose karta hai jo tum chaho.

## 3. Requests handle karo
Backend tumhe ye bhejega jab Claude `device_call` bolega:
```json
{"type": "request", "id": "abc123", "method": "CALL", "path": "/cap/system_info", "body": {...arguments...}}
```
`path` ka aakhri hissa hi capability ka naam hai. Jawab isi `id` ke saath:
```json
{"type": "response", "id": "abc123", "status": 200, "contentType": "application/json", "body": "{\"cpu\":12}"}
```
- `status` 200-299 = success, 400+ = error (Claude ko `isError` ke saath dikhta hai)
- Image bhejni ho to `body` ki jagah `bodyBase64` + `contentType: image/...`
- Backend 25 second baad khud timeout kar dega agar jawab na aaye

## 4. Suggested capabilities (naam khud choose karo, ye sirf idea hain)
`system_info` (READ jaisa), `run_command` (HIGH_RISK jaisa — sirf tab implement karo
jab genuinely zaroorat ho, aur khud confirm-jaisi soch rakho), `list_files`, `open_app`.

## 5. Suraksha zimmedari agent ki apni hai
Backend sirf pehchaan (token) aur naam-matching karta hai — capability ke andar
kya hota hai (kaunsa command chalta hai) wo poori tarah agent ka apna kaam hai.
Android app mein jo Stage 8 (scopes/confirm/audit/Emergency Stop) hai, wo
Android-specific hai — naya agent likhte waqt apni zaroorat ke hisaab se
apne scope/confirmation rules khud banao.
