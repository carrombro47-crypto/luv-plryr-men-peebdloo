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

// ── Force Check Now button ──────────────────────────────────────────────
// Background watcher already automatically shuru ho chuka hota hai jaise
// hi link generate hua tha (live end hote hi khud download+upload karega).
// Ye button sirf ek manual "kick" hai — agar kisi wajah se watcher active
// nahi mila (e.g. app abhi-abhi restart hua ho) to use idempotently
// dobara ensure/start kar deta hai.
const recordBtn = document.getElementById("recordBtn");
if (recordBtn) {
  recordBtn.addEventListener("click", async () => {
    const name = recordBtn.getAttribute("data-name");
    recordBtn.disabled = true;
    recordBtn.textContent = "⏳ CHECKING…";
    try {
      const res = await fetch("/api/record/" + encodeURIComponent(name), {
        method: "POST",
      });
      const data = await res.json();
      if (data.ok) {
        recordBtn.textContent = "✅ WATCHER ACTIVE";
        showToast(data.note || "Watcher active hai — live end hote hi auto process hoga ✅");
      } else {
        recordBtn.textContent = "⚡ FORCE CHECK NOW";
        recordBtn.disabled = false;
        showToast("❌ " + (data.error || "Failed"));
      }
    } catch (err) {
      recordBtn.textContent = "⚡ FORCE CHECK NOW";
      recordBtn.disabled = false;
      showToast("❌ " + err.message);
    }
  });
}
