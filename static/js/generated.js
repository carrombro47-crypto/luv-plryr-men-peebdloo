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
