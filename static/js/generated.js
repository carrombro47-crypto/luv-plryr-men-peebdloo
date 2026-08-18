const toast = document.getElementById("toast");

function showToast(message, duration = 3000) {
  toast.textContent = message;
  toast.classList.remove("hidden");
  setTimeout(() => toast.classList.add("hidden"), duration);
}

document.querySelectorAll(".copy-btn").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const text = btn.getAttribute("data-copy");
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    showToast("Copied ✅!");
  });
});

// ── Start Recording button ──
const recordBtn = document.getElementById("recordBtn");
if (recordBtn) {
  recordBtn.addEventListener("click", async () => {
    const name = recordBtn.getAttribute("data-name");
    recordBtn.disabled = true;
    recordBtn.textContent = "⏺ STARTING…";
    try {
      const res = await fetch("/api/record/" + encodeURIComponent(name), {
        method: "POST",
      });
      const data = await res.json();
      if (data.ok) {
        recordBtn.textContent = "⏺ RECORDING… ✅";
        showToast("Recording start ho gayi — live end hone par auto process hogi ✅");
      } else {
        recordBtn.textContent = "⏺ START RECORDING";
        recordBtn.disabled = false;
        showToast("❌ " + (data.error || "Failed"));
      }
    } catch (err) {
      recordBtn.textContent = "⏺ START RECORDING";
      recordBtn.disabled = false;
      showToast("❌ " + err.message);
    }
  });
}

// ── Retry Telegram Upload button ──
// Recording/480p file already ban chuka hota hai lekin agar Telegram upload
// fail ho gaya ho (BOT_TOKEN/CHAT_ID env missing tha, file size limit,
// ya transient network error) — poori class dobara record kiye bina sirf
// upload dobara try karo.
const retryUploadBtn = document.getElementById("retryUploadBtn");
const statusText = document.getElementById("statusText");

async function refreshStatus() {
  const name = retryUploadBtn ? retryUploadBtn.getAttribute("data-name") : null;
  if (!name) return;
  try {
    const res = await fetch("/api/status/" + encodeURIComponent(name));
    const data = await res.json();
    if (statusText) statusText.textContent = data.status || "";
    if (retryUploadBtn) {
      retryUploadBtn.style.display = data.status === "UPLOAD_FAILED" ? "inline-block" : "none";
    }
  } catch {
    /* ignore — next poll will retry */
  }
}

if (retryUploadBtn) {
  retryUploadBtn.addEventListener("click", async () => {
    const name = retryUploadBtn.getAttribute("data-name");
    retryUploadBtn.disabled = true;
    retryUploadBtn.textContent = "⬆ RETRYING…";
    try {
      const res = await fetch("/api/retry-upload/" + encodeURIComponent(name), {
        method: "POST",
      });
      const data = await res.json();
      if (data.ok) {
        showToast("Upload dobara try ho raha hai — thodi der me status update hoga ✅");
      } else {
        showToast("❌ " + (data.error || "Failed"));
      }
    } catch (err) {
      showToast("❌ " + err.message);
    } finally {
      retryUploadBtn.disabled = false;
      retryUploadBtn.textContent = "⬆ RETRY TELEGRAM UPLOAD";
    }
  });
  // Har 5s status check karo taaki UPLOAD_FAILED hone par button khud dikhe.
  refreshStatus();
  setInterval(refreshStatus, 5000);
}
