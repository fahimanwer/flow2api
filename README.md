# Flow2API

<div align="center">

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/fastapi-0.119.0-green.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/docker-supported-blue.svg)](https://www.docker.com/)

**A full-featured OpenAI-compatible API service that gives Flow a unified interface**

</div>

## ❤️ Sponsors

<div align="center">

[![FastAIToken](static/sponsors/fastaitoken-banner.png)](https://www.fastaitoken.com/register)

</div>

**FastAIToken** is an AI API aggregation platform for developers. It supports mainstream models such as OpenAI, Claude and Gemini, is compatible with the OpenAI API protocol, and plugs straight into AI dev tools such as **Claude Code, Codex, Gemini CLI, Cherry Studio, Cline and Continue**. Top-ups are **1:1 (1 CNY = 1 USD of API credit)**, helping developers use leading models at lower cost and higher efficiency.

The platform offers several selectable groups and a public status page, so developers can pick channels by cost, speed and stability, and get **24/7 human technical support** (not a bot).

**Mostly doing AI development integrations? Try [FastAIToken](https://www.fastaitoken.com/register) — it works with Codex / Claude Code / Gemini CLI and other mainstream tools.**

---

<table>
<tr>
<td width="180" align="center" valign="middle">
  <a href="https://www.fastaitoken.com/register">
    <img src="static/sponsors/fastaitoken-logo.png" alt="FastAIToken" width="150">
  </a>
</td>
<td valign="top">
  Thanks to <strong>FastAIToken</strong> for sponsoring this project! FastAIToken is an AI API aggregation platform for developers, compatible with the OpenAI API protocol, supporting mainstream AI dev tools such as Claude Code, Codex, Gemini CLI, Cherry Studio, Cline and Continue.<br><br>
  Currently offers a <strong>0.02x OpenAI promo group (limited time)</strong>, <strong>0.25x OpenAI standard group</strong>, <strong>0.35x OpenAI backup group</strong>, <strong>0.45x OpenAI Pro group</strong>, <strong>0.7x Claude standard group</strong> and <strong>1.2x Claude Max channel</strong>; top-ups at <strong>1 CNY = 1 USD of API credit</strong>, plus a public status page, business invoicing, a <strong>99% SLA enterprise-grade stable account pool</strong> and <strong>24/7 human technical support</strong>.<br><br>
  Sign up via <a href="https://www.fastaitoken.com/register">this link</a> to try it.
</td>
</tr>
</table>

## ✨ Key features

- 🎨 **Text-to-image** / **image-to-image**
- 🎬 **Text-to-video** / **image-to-video**
- 🎞️ **First/last-frame video**
- 🔄 **AT/ST auto-refresh** - AT refreshes automatically when it expires; an expired ST is renewed through the browser (personal mode)
- 📊 **Balance display** - live lookup and display of VideoFX Credits
- 🚀 **Load balancing** - round-robin across multiple tokens, with concurrency control
- 🌐 **Proxy support** - HTTP/SOCKS5 proxies
- 📱 **Web admin panel** - simple token and settings management
- 🎨 **Multi-turn image generation**
- 🧩 **Official Gemini request format** - supports `generateContent` / `streamGenerateContent`, `systemInstruction`, `contents.parts.text/inlineData/fileData`
- ✅ **Official Gemini format tested with real images** - verified with a real token that `/models/{model}:generateContent` returns the official `candidates[].content.parts[].inlineData`

## 🚀 Quick start

### Requirements

- Docker and Docker Compose (recommended)
- Or Python 3.8+

- Flow added an extra captcha, so you can choose browser-based solving or a third-party solver:
Sign up at [YesCaptcha](https://yescaptcha.com/i/13Xd8K), get an API key, and enter it in the ```YesCaptcha API key``` field on the system settings page
- YesCaptcha `type` can be switched in the admin panel: `RecaptchaV3TaskProxyless`, `RecaptchaV3TaskProxylessM1`, `RecaptchaV3TaskProxylessM1S7`, `RecaptchaV3TaskProxylessM1S9`; `M1S9` is the current default recommendation. S7/S9 force `minScore` 0.7/0.9.
- The default `docker-compose.yml` is meant to be used with a third-party solver (yescaptcha/capmonster/ezcaptcha/capsolver).
For headed browser solving inside Docker (browser/personal), use `docker-compose.headed.yml` below.

- Browser extension that auto-updates the ST: [Flow2API-Token-Updater](https://github.com/TheSmallHanCat/Flow2API-Token-Updater)

### Option 1: Docker (recommended)

#### Standard mode (no proxy)

```bash
# Clone the project
git clone https://github.com/TheSmallHanCat/flow2api.git
cd flow2api

# Start the service
docker-compose up -d

# View logs
docker-compose logs -f
```

> Note: Compose mounts `./tmp:/app/tmp` by default. A cache timeout of `0` means "never auto-delete"; to keep cached files after the container is rebuilt, you also need to keep this `tmp` mount.

#### WARP mode (with proxy)

```bash
# Start with the WARP proxy
docker-compose -f docker-compose.proxy.yml up -d

# View logs
docker-compose -f docker-compose.proxy.yml logs -f
```

#### Docker headed captcha mode (browser / personal)

> For when you need a virtual desktop and want headed browser captcha solving inside the container.  
> This mode starts `Xvfb + Fluxbox` for an in-container display and sets `ALLOW_DOCKER_HEADED_CAPTCHA=true`.  
> Only the app port is exposed; no remote desktop port is provided.
> The built-in `personal` browser now starts headed by default; to switch back to headless temporarily, set the env var `PERSONAL_BROWSER_HEADLESS=true`.

```bash
# Start headed mode (use --build the first time)
docker compose -f docker-compose.headed.yml up -d --build

# View logs
docker compose -f docker-compose.headed.yml logs -f
```

- API port: `8000`
- In the admin panel, set the captcha method to `browser` or `personal`

### Option 2: Local install

```bash
# Clone the project
git clone https://github.com/TheSmallHanCat/flow2api.git
cd flow2api

# Create a virtual environment
python -m venv venv

# Activate the virtual environment
# Windows
venv\Scripts\activate
# Linux/Mac
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Start the service
python main.py
```

### First login

Once the service is running, open the admin panel at **http://localhost:8000**. Change the password right after your first login!

- **Username**: `admin`
- **Password**: `admin`

## 📈 Monitoring endpoints

- `GET /health`: public health check; returns a summary: whether the service is up, active tokens, tokens about to expire, expired tokens, tokens disabled by 429, etc.
- `GET /metrics`: Prometheus metrics
- `GET /api/tokens`: admin endpoint; returns token status such as `at_expires`, `at_expired`, `at_expiring_within_1h`, `ban_reason`, `consecutive_error_count`

Prometheus can scrape `/metrics` directly. On Kubernetes, scrape it only from inside the cluster and block outside access to `/metrics` at the Ingress/Gateway.

### Model test page

Open **http://localhost:8000/test** for the built-in model test page. It can:

- Browse every model grouped by family (Nano Banana 2.1 / Pro / 2 Lite, Omni 1.1 Flash, Veo 3.1 Fast / Quality / Lite) with its readable name; image limits come from `/v1/models`
- Test with one click from a prompt, with streamed progress
- Upload images for image-to-image / image-to-video
- Preview the image or video when generation finishes

## 📋 Supported models

The full, always-current list is **`GET /v1/models`** (and the `/test` page). Each entry keeps its `id`
(what you send) and adds a readable `name`, `family`, `google_model`, `mode`, `orientation`,
`duration_seconds`, `resolution`, `accounts`, `inputs` and `aliases`. Names, aliases, modes, account
rules and credit costs are explained in **[docs/models.md](docs/models.md)**.

| Family (Google's name) | Ids you can send (old ids keep working) |
|---|---|
| Nano Banana 2.1 (`gemini-nano-banana-2.1`) | `nano-banana-2.1-{aspect}[-2k\|-4k]` = `gemini-3.1-flash-image-{aspect}[-2k\|-4k]` |
| Nano Banana Pro (`gemini-3-pro-image`) | `nano-banana-pro-{aspect}[-2k\|-4k]` = `gemini-3.0-pro-image-{aspect}[-2k\|-4k]` |
| Nano Banana 2 Lite (`gemini-3.1-flash-lite-image`) | `nano-banana-2-lite-{aspect}` (1K only) |
| Omni 1.1 Flash (`gemini-omni-1.1-flash`) | `omni-1.1-flash-{4,6,8,10}s-{landscape,portrait}[-1080p]` = `omni_{d}s[_portrait][_1080p]`; ingredients (every image is a reference): `omni-1.1-flash-ingredients-…` = `omni_r2v_…` |
| Veo 3.1 Fast / Quality / Lite | `veo_3_1_t2v…` (text), `veo_3_1_i2v…` (frames), `veo_3_1_r2v…` (ingredients), `…_1080p` / `…_4k` (upscaled), `veo_3_1_extend…` |

`{aspect}` = `landscape`, `portrait`, `square`, `four-three`, `three-four`. `gemini-3.1-flash-image-*`
runs **Nano Banana 2.1**: Google's Nano Banana 2 was retired in Flow on 2026-10-06 and the id was kept so
no caller breaks. All `*_relaxed` Veo ids are retired by Google (marked in `/v1/models`).

## 📡 API examples (streaming required)

> Besides the `OpenAI-compatible` examples below, the service also supports the official Gemini format:
> - `POST /v1beta/models/{model}:generateContent`
> - `POST /models/{model}:generateContent`
> - `POST /v1beta/models/{model}:streamGenerateContent`
> - `POST /models/{model}:streamGenerateContent`
>
> The official Gemini format accepts these auth methods:
> - `Authorization: Bearer <api_key>`
> - `x-goog-api-key: <api_key>`
> - `?key=<api_key>`
>
> Supported fields in the official Gemini image request body:
> - `systemInstruction`
> - `contents[].parts[].text`
> - `contents[].parts[].inlineData`
> - `contents[].parts[].fileData.fileUri`
> - `generationConfig.responseModalities`
> - `generationConfig.imageConfig.aspectRatio`
> - `generationConfig.imageConfig.imageSize`

### Official Gemini generateContent (text-to-image)

> Tested and working with a real token.
> For a streamed response, change the path to `:streamGenerateContent?alt=sse`.

```bash
curl -X POST "http://localhost:8000/models/gemini-3.1-flash-image:generateContent" \
  -H "x-goog-api-key: han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "systemInstruction": {
      "parts": [
        {
          "text": "Return an image only."
        }
      ]
    },
    "contents": [
      {
        "role": "user",
        "parts": [
          {
            "text": "A red apple on a wooden table, studio lighting, minimal background"
          }
        ]
      }
    ],
    "generationConfig": {
      "responseModalities": ["IMAGE"],
      "imageConfig": {
        "aspectRatio": "1:1",
        "imageSize": "1K"
      }
    }
  }'
```

### Text-to-image

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image-landscape",
    "messages": [
      {
        "role": "user",
        "content": "A cute cat playing in a garden"
      }
    ],
    "stream": true
  }'
```

### Image-to-image

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image-landscape",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "Turn this image into a watercolor painting"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<base64_encoded_image>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

### Text-to-video

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo_3_1_t2v_fast_landscape",
    "messages": [
      {
        "role": "user",
        "content": "A kitten chasing a butterfly on the grass"
      }
    ],
    "stream": true
  }'
```

### Video from first and last frames

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo_3_1_i2v_s_fast_fl_landscape",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "Transition from the first image to the second"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<first_frame_base64>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<last_frame_base64>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

### Video from multiple images

> For `R2V` the server builds the new video request body for you; callers keep sending OpenAI-compatible input.
> The server maps landscape `R2V` to the latest `*_landscape` upstream model key automatically.
> Currently at most **3 reference images**.

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo_3_1_r2v_fast_portrait",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "Using the people and scene from the three reference images, make a portrait video with a smooth push-in camera move"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<reference_image_1_base64>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<reference_image_2_base64>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<reference_image_3_base64>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

---

## 📄 License

This project is under the MIT License. See the [LICENSE](LICENSE) file.

---

## 🙏 Thanks

- [PearNoDec](https://github.com/PearNoDec) for the YesCaptcha solving approach
- [raomaiping](https://github.com/raomaiping) for the headless solving approach
Thanks to all contributors and users for their support!

---

## 📞 Contact

- Open an issue: [GitHub Issues](https://github.com/TheSmallHanCat/flow2api/issues)

---

**⭐ If this project helps you, please give it a star!**

## Recent updates

- `9f1d712` Sync personal captcha logic, including cleanup, browser args and captcha-method settings.
- `da2ad06` Merge PR #133.
- `abd0c00` Fix integration issues after merging PR #133.
- `55431c9` Sync origin/main into PR #133.
- `4b7a0ad` Add Prometheus service metrics and token health monitoring.

## Star History

[![Star History Chart](https://star-history.dera.page/svg?repos=TheSmallHanCat/flow2api&type=date&legend=top-left)](https://star-history.dera.page/#TheSmallHanCat/flow2api&type=date&legend=top-left)

## Characters (consistent people / objects)

Send `characters: [{"name": "Maya", "images": [...]}]` with a request and write `@Maya` in the prompt. See [docs/flow-characters.md](docs/flow-characters.md).
