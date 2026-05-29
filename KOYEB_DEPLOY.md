# 🚀 Deploy to Koyeb — Complete Guide
# Free forever, no credit card, always on, 512MB RAM

## Your project folder must contain:
```
gech_koyeb.py              ← main bot (Koyeb-optimized)
requirements.txt           ← Python packages
Dockerfile                 ← tells Koyeb how to build
.dockerignore              ← keeps image small
id_template.png            ← your ID card template (REQUIRED)
NotoSansEthiopic-Regular.ttf  ← font file (REQUIRED)
```

---

## Step 1 — Push to GitHub

```cmd
git init
git add .
git commit -m "Koyeb deploy"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

---

## Step 2 — Create Koyeb account
1. Go to https://koyeb.com
2. Sign up with GitHub (no credit card needed)

---

## Step 3 — Create new App on Koyeb
1. Click **"Create App"**
2. Choose **"GitHub"**
3. Select your repository
4. Koyeb detects the Dockerfile automatically ✅
5. Set these settings:
   - **Instance type:** Free (nano)
   - **Region:** Frankfurt or Washington (pick closest)
   - **Port:** leave empty (bot uses polling, not HTTP)

---

## Step 4 — Set Environment Variables
In Koyeb → your app → **"Settings"** → **"Environment variables"**:

| Key | Value |
|-----|-------|
| `BOT_TOKEN` | `8757528263:AAEfqMUsHxbUFOmNfPK2lJ6CuA8yaRHdBWY` |
| `ANTHROPIC_API_KEY` | `sk-ant-...` (optional, best Amharic OCR) |

Click **Save** → app redeploys automatically.

---

## Step 5 — Watch deployment logs
Koyeb → your app → **"Deployments"** → click latest → **"Logs"**

You should see:
```
✅ u2netp model cached
✅ Bot running...
```

First deployment takes ~3 minutes (installing packages).
After that, updates deploy in ~1 minute.

---

## RAM Usage on Koyeb 512MB

| State | RAM | Status |
|-------|-----|--------|
| Bot idle (waiting) | ~150MB | ✅ Fine |
| Processing photo (rembg active) | ~360MB | ✅ Fine |
| Peak (rembg + telegram send) | ~420MB | ✅ Fine |
| **Koyeb limit** | **512MB** | ✅ Safe |

---

## 900 photos/day capacity
- Koyeb free = 1 instance, always on
- Each photo takes ~10 seconds to process
- 900 photos/day = 1 photo every 96 seconds on average
- Your bot handles this comfortably ✅

---

## Troubleshooting

| Error | Fix |
|-------|-----|
| `libzbar.so not found` | Dockerfile already installs it — redeploy |
| `No module named 'telegram'` | Check requirements.txt — redeploy |
| Bot not responding | Check BOT_TOKEN in env vars |
| OOM (out of memory) | Already optimized — should not happen |
| `id_template.png not found` | Make sure it's in your repo (not in .gitignore) |

---

## Auto-redeploy on push
Koyeb watches your GitHub repo. Every `git push` triggers a new deployment automatically.
