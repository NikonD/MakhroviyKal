const $ = (id) => document.getElementById(id);
const toast = (msg, kind = "") => {
  const t = $("toast");
  t.textContent = msg;
  t.className = `toast ${kind}`;
  setTimeout(() => t.classList.add("hidden"), 4000);
  t.classList.remove("hidden");
};

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

async function loadRequests() {
  try {
    const res = await fetch("/api/requests");
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    const list = $("reqList");
    list.innerHTML = "";
    const items = data?.items || [];
    $("emptyState").classList.toggle("hidden", items.length !== 0);
    items.forEach((r) => {
      const card = document.createElement("div");
      card.className = "req-card";
      const title = escapeHtml(r.student_name || r.filename || ("Заявление #" + r.id));
      const driveUrl = r.drive_file_url || (r.drive_file_id
        ? `https://drive.google.com/file/d/${encodeURIComponent(r.drive_file_id)}/view`
        : "");
      const driveLink = driveUrl
        ? `<a class="req-drive-link" href="${escapeHtml(driveUrl)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(r.filename || "PDF")}">PDF на Google Drive</a>`
        : "";
      card.innerHTML = `
        <a class="req-card-main" href="/?request=${encodeURIComponent(r.id)}">
          <div class="req-top">
            <div class="req-title">${title}</div>
            <span class="pill ${r.status === "pending_approval" ? "" : "success"}">${escapeHtml(r.status)}</span>
          </div>
          <div class="req-meta">
            <div class="meta">${escapeHtml(r.group || "")}</div>
            <div class="meta">#${escapeHtml(r.id)} · ${escapeHtml(r.filename || "")}</div>
          </div>
        </a>
        ${driveLink}
      `;
      list.appendChild(card);
    });
  } catch (e) {
    toast("Ошибка загрузки списка: " + e.message, "error");
  }
}

$("refreshBtn").addEventListener("click", () => loadRequests());
loadRequests();

