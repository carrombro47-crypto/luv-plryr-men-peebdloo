// ── STRICT ADMIN LOGIN PORTAL ───────────────────────────────────────────
// Real fix: the Owner Name / Admin Key / VIP Key are validated on the
// server only (see /login in main.py). This file never holds those
// values — there is nothing here for DevTools/Sources/eruda to reveal,
// no matter how the page is inspected.

const loginGate = document.getElementById("loginGate");
const generateWrapper = document.getElementById("generateWrapper");
const ownerNameInput = document.getElementById("ownerNameInput");
const adminKeyInput = document.getElementById("adminKeyInput");
const vipKeyInput = document.getElementById("vipKeyInput");
const loginError = document.getElementById("loginError");
const loginBtn = document.getElementById("loginBtn");

loginBtn.addEventListener("click", async () => {
  loginBtn.disabled = true;
  try {
    const res = await fetch("/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        owner_name: ownerNameInput.value,
        admin_key: adminKeyInput.value,
        vip_key: vipKeyInput.value,
      }),
    });
    const data = await res.json();

    if (data.ok) {
      loginError.classList.add("hidden");
      loginGate.classList.add("hidden");
      generateWrapper.classList.remove("hidden");
    } else {
      loginError.textContent = "❌ " + (data.error || "Invalid Name / Admin Key / VIP Key. Check karo aur dobara try karo.");
      loginError.classList.remove("hidden");
    }
  } catch (err) {
    loginError.textContent = "❌ Something went wrong: " + err.message;
    loginError.classList.remove("hidden");
  } finally {
    loginBtn.disabled = false;
  }
});

// ── Link-generate form ──────────────────────────────────────────────────
const generateForm = document.getElementById("generateForm");
const originalLinkInput = document.getElementById("originalLinkInput");
const lectureNameInput = document.getElementById("lectureNameInput");
const nameError = document.getElementById("nameError");
const generateBtn = document.getElementById("generateBtn");
const statusBox = document.getElementById("statusBox");
const toast = document.getElementById("toast");

// ── Source type toggle: Live stream vs Already Recorded (VOD) ──
const modeLiveBtn = document.getElementById("modeLiveBtn");
const modeVodBtn = document.getElementById("modeVodBtn");
const linkLabel = document.getElementById("linkLabel");
const modeHint = document.getElementById("modeHint");
let sourceType = "live";

function setMode(mode) {
  sourceType = mode;
  modeLiveBtn.classList.toggle("active", mode === "live");
  modeVodBtn.classList.toggle("active", mode === "vod");
  if (mode === "live") {
    linkLabel.textContent = "PASTE ORIGINAL M3U8 LINK HERE:";
    originalLinkInput.placeholder = "https://...index.m3u8?Signature=...";
    modeHint.textContent = "Live stream ka index.m3u8 URL — end hote hi system khud detect karke record/process karega aur direct download ready kar dega.";
  } else {
    linkLabel.textContent = "PASTE RECORDED (master.m3u8 / mp4) LINK HERE:";
    originalLinkInput.placeholder = "https://...master.m3u8 ya https://...video.mp4";
    modeHint.textContent = "Already-recorded playable URL — seedha download+480p processing shuru ho jaayega, live-wait ki zaroorat nahi.";
  }
}
modeLiveBtn.addEventListener("click", () => setMode("live"));
modeVodBtn.addEventListener("click", () => setMode("vod"));

// Letters (any script incl. Hindi) + digits + hyphen only. No spaces, no
// underscore, no emoji, no special characters — same rule the backend
// applies as a safety net too.
const NAME_RE = /^[\p{L}\p{N}-]{1,100}$/u;

function showToast(message, duration = 3000) {
  toast.textContent = message;
  toast.classList.remove("hidden");
  setTimeout(() => toast.classList.add("hidden"), duration);
}

generateForm.addEventListener("submit", async (e) => {
  e.preventDefault();

  const originalUrl = originalLinkInput.value.trim();
  const rawName = lectureNameInput.value.trim().replace(/\s+/g, "-");

  nameError.classList.add("hidden");
  statusBox.classList.add("hidden");

  if (!originalUrl || !/^https?:\/\//i.test(originalUrl)) {
    statusBox.textContent = "❌ Valid http(s) link paste karo.";
    statusBox.classList.remove("hidden");
    statusBox.classList.add("error");
    return;
  }

  if (!NAME_RE.test(rawName)) {
    nameError.textContent = "Naam sirf letters, numbers aur hyphen(-) allowed hai — spaces, emoji ya special characters nahi chalenge.";
    nameError.classList.remove("hidden");
    return;
  }

  generateBtn.disabled = true;
  generateBtn.textContent = "⏳ Generating...";

  try {
    const res = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ original_url: originalUrl, name: rawName, source_type: sourceType }),
    });
    const data = await res.json();

    if (!data.ok) {
      if (res.status === 401) {
        generateWrapper.classList.add("hidden");
        loginGate.classList.remove("hidden");
        loginError.textContent = "❌ Session expired. Dobara login karo.";
        loginError.classList.remove("hidden");
        generateBtn.disabled = false;
        generateBtn.textContent = "GENERATE LINK ⚡";
        return;
      }
      statusBox.textContent = "❌ " + data.error;
      statusBox.classList.remove("hidden");
      statusBox.classList.add("error");
      generateBtn.disabled = false;
      generateBtn.textContent = "GENERATE LINK ⚡";
      return;
    }

    showToast("Link generated ✅!");
    setTimeout(() => {
      window.location.href = "/generated/" + data.name;
    }, 600);

  } catch (err) {
    statusBox.textContent = "❌ Something went wrong: " + err.message;
    statusBox.classList.remove("hidden");
    statusBox.classList.add("error");
    generateBtn.disabled = false;
    generateBtn.textContent = "GENERATE LINK ⚡";
  }
});
