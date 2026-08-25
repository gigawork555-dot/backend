// static/portal/assets/common.js
// Shared across all portal pages. Each page is now a real, separate
// URL — this file just centralizes auth storage + API calls so login
// state, logout, and 401-handling aren't duplicated 5 times.

const API_BASE = "";
const PORTAL_BASE = "/portal";

// ── Auth storage (sessionStorage — cleared when the tab closes) ────
function getToken() { return sessionStorage.getItem("jwt"); }
function setToken(t) { sessionStorage.setItem("jwt", t); }
function getRole() { return sessionStorage.getItem("role"); }
function setRole(r) { sessionStorage.setItem("role", r); }
function getUsername() { return sessionStorage.getItem("username"); }
function setUsername(u) { sessionStorage.setItem("username", u); }

function clearSession() {
  sessionStorage.removeItem("jwt");
  sessionStorage.removeItem("role");
  sessionStorage.removeItem("username");
}

function logout() {
  clearSession();
  window.location.href = `${PORTAL_BASE}/`;
}

// ── Topbar (same markup injected into every page's #topbarActions) ─
function renderTopbar() {
  const el = document.getElementById("topbarActions");
  if (!el) return;

  if (!getToken()) {
    el.innerHTML = `
      <a class="btn ghost" href="${PORTAL_BASE}/register.html">สมัครสมาชิก</a>
      <a class="btn" href="${PORTAL_BASE}/login.html">เข้าสู่ระบบ</a>`;
  } else {
    const dashboardUrl = getRole() === "admin"
      ? `${PORTAL_BASE}/admin.html`
      : `${PORTAL_BASE}/dashboard.html`;
    el.innerHTML = `
      <span class="who">${getUsername() || ""} (${getRole()})</span>
      <a class="btn ghost" href="${dashboardUrl}">แดชบอร์ด</a>
      <button class="secondary" onclick="logout()">ออกจากระบบ</button>`;
  }
}

// ── Route guards — call at top of protected/guest-only pages ───────

/** Call on dashboard.html / admin.html. Redirects to login if not
 *  authenticated, or to the correct dashboard if the role doesn't match. */
function requireRole(expectedRole) {
  if (!getToken()) {
    window.location.href = `${PORTAL_BASE}/login.html`;
    return false;
  }
  if (getRole() !== expectedRole) {
    const correct = getRole() === "admin"
      ? `${PORTAL_BASE}/admin.html`
      : `${PORTAL_BASE}/dashboard.html`;
    window.location.href = correct;
    return false;
  }
  return true;
}

/** Call on login.html / register.html. If already logged in, skip
 *  straight to the dashboard instead of showing the form again. */
function redirectIfLoggedIn() {
  if (getToken()) {
    window.location.href = getRole() === "admin"
      ? `${PORTAL_BASE}/admin.html`
      : `${PORTAL_BASE}/dashboard.html`;
    return true;
  }
  return false;
}

// ── Authenticated fetch wrapper — auto-attaches JWT, auto-logout on 401 ─
async function apiFetch(path, opts = {}) {
  const resp = await fetch(API_BASE + path, {
    ...opts,
    headers: {
      ...(opts.headers || {}),
      Authorization: "Bearer " + getToken(),
    },
  });
  if (resp.status === 401) {
    logout();
    throw new Error("unauthorized — redirected to login");
  }
  return resp;
}

// ── Copy-to-clipboard helper (secure-context aware, with fallback) ─
function copyTextToButton(text, btn) {
  function feedback() {
    const original = btn.textContent;
    btn.textContent = "คัดลอกแล้ว ✓";
    setTimeout(() => { btn.textContent = original; }, 1500);
  }
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(feedback).catch(() => alert("คัดลอกไม่สำเร็จ"));
  } else {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); feedback(); }
    catch (e) { alert("คัดลอกไม่สำเร็จ กรุณาเลือกข้อความแล้วกด Ctrl+C ด้วยตนเอง"); }
    document.body.removeChild(ta);
  }
}

document.addEventListener("DOMContentLoaded", renderTopbar);
