# Control Hub Relay + MCP Server

Ye chhota FastAPI server Railway pe chalta hai aur do kaam karta hai:

1. **Phone** iss se outbound WebSocket (`/device`) se jud jaata hai
2. **Claude.ai connector** iss ke `/mcp/<MCP_SECRET>` URL pe MCP tool calls bhejta hai,
   server unhe phone tak pahunchata hai aur jawab wapas deta hai

```
Claude.ai  --HTTPS-->  Railway (/mcp/<secret>)  --WebSocket-->  Phone (Control Hub app)
                                                                   |
                                                             127.0.0.1:8080 (Local API)
                                                             -> scopes / confirm / audit / Emergency Stop
```

## Step 1 — Repo mein daalo
Poora `ControlHub` folder GitHub repo mein daalo (root mein `app/`, `backend/`, `.github/`).

## Step 2 — Railway pe deploy
1. railway.app → **New Project → Deploy from GitHub repo** → apna repo chuno
2. Service → **Settings → Root Directory** = `backend`
3. Service → **Variables** mein ye 2 add karo:

| Variable | Value |
|---|---|
| `MCP_SECRET` | 40+ random characters (sirf letters + digits) — ye connector URL ka hissa banega |
| `DEVICE_TOKEN` | 40+ random characters (sirf letters + digits) — sirf phone ke paas |

   Ye dono, aur har device ka token, alag-alag hone chahiye. Kisi password generator se
   bana lo (terminal ki zaroorat nahi). 24 characters se chhota hoga to us hisse ko
   server ignore kar dega (jaanbujh ke).

   **(Optional, Feature #59) Ek se zyada device:** `DEVICE_TOKEN` ki jagah/saath
   `DEVICES` variable use karo — `id:platform:token,id:platform:token,...`
   (platform = `android`/`windows`/`linux`/`macos`). Windows/Linux/macOS ke liye
   khud koi "agent" code is project mein nahi hai — `backend/DEVICE_PROTOCOL.md`
   mein likha hai wo kaise banaya jaaye.
4. **Settings → Networking → Generate Domain** — tumhe `https://xxxx.up.railway.app` milega

Check: browser mein `https://xxxx.up.railway.app/` kholo. Aisa dikhna chahiye:
`{"service":"control-hub-relay","configured":true,"device_connected":false,...}`
(`configured:false` matlab variables sahi nahi daale.)

## Step 3 — Phone setup
1. Control Hub kholo → **Enable Accessibility** → Control Hub ko ON karo
2. **Settings** → *Remote relay* mein:
   - URL = `https://xxxx.up.railway.app`
   - Device token = wahi `DEVICE_TOKEN`
   - **Save relay settings**
3. Dashboard pe **Start API Server**
4. Settings mein status **Relay: CONNECTED** dikhna chahiye, aur backend ka `/` page
   `"device_connected": true` dikhaye

## Step 4 — Claude.ai mein connector
Claude.ai → **Settings → Connectors → Add custom connector**
(menu ke naam thoda alag ho sakte hain, aur ye har plan pe available na ho — apni settings check karna)
- URL: `https://xxxx.up.railway.app/mcp/<MCP_SECRET>`

Phir kisi chat mein connector ON karo aur try karo:
- "Meri phone ka status check karo"  (`device_status`)
- "Phone pe kaunse apps installed hain?"  (`list_apps`)
- "Phone ki screen dekho aur batao kya dikh raha hai"  (`get_scene` / `screenshot`)

## Kya-kya tools milte hain (16)
`device_status`, `get_capabilities`, `list_apps`, `app_status`, `launch_app`, `stop_app`*,
`read_screen`, `get_scene`, `screenshot`, `tap`, `type_text`*, `scroll`, `press_back`, `press_home`,
`call_module`* (Feature #57 SDK modules — phone Settings mein jo bhi module registered ho),
`device_call` (Feature #59 — non-Android agents ke liye, unki khud batayi capabilities tak seemit)

(* = high-risk, `confirm=true` chahiye — Claude ko sirf tumhari saaf ijazat ke baad dena chahiye)

Har phone-tool (device_status/device_call chhodke) ek optional `device` argument bhi
leta hai — sirf tab dena zaroori hai jab ek se zyada device connected ho.

## Zaroori suraksha baatein
- **Connector URL ek password hai.** Jiske paas URL hai wo phone pe actions chala sakta hai
  (Claude.ai ke custom connectors abhi static header-token nahi lete, isliye secret URL mein hai).
  URL kisi ko mat do, screenshot mein mat dikhao. Leak ho jaye to Railway mein `MCP_SECRET` badlo.
- `confirm=true` ek *request-level* check hai — jiske paas URL hai wo ise khud bhi bhej sakta hai.
  Asli rok-tham phone ki **Settings mein Scope switches** hain (HIGH RISK OFF karo agar zaroorat nahi)
  aur Dashboard ka **Emergency Stop** (ye sirf phone se hi hataya ja sakta hai).
- Server logs mein URL na aaye isliye `--no-access-log` laga hai (Procfile).

## Limits (sach-sach)
- Claude sirf tab kaam karta hai jab tum chat mein bologe — ye 24/7 apne aap chalne wala agent nahi hai.
- Tap / type / screenshot ke liye phone **awake aur unlocked** hona chahiye.
- Screen off rehne par Android connection kaat sakta hai → Settings mein
  *Open battery settings* se Control Hub ko Unrestricted karo.
- Railway ka free tier permanent nahi hota — 24/7 chalne ke liye paid/credit plan lagega (current pricing check karna).

## Troubleshooting
| Dikkat | Wajah / Hal |
|---|---|
| `/` par `configured:false` | `MCP_SECRET` / `DEVICE_TOKEN` nahi daale ya 24 char se chhote hain |
| Phone: `WAITING_RETRY (HTTP 403)` | Device token backend wale se match nahi karta |
| Phone: `WAITING_RETRY (Failed to connect...)` | Phone ka internet / URL galat |
| Claude: "phone is not connected" | Dashboard pe Start API Server dabao; battery restriction hatao |
| Claude: 503 "Accessibility Service is not running" | Accessibility ON karo |
| Claude: 403 "Scope ... disabled" | Settings mein wo scope switch ON karo |
| Connector add nahi ho raha | URL `https://` se shuru ho aur `/mcp/<secret>` par khatam; `/` page pehle browser mein check karo |

## Local test (optional)
`python3 test_backend.py` — protocol/relay/multi-device logic ke 90 checks (sirf Python chahiye).
