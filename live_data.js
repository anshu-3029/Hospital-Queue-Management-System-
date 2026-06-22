// ============================================================
// live_data.js
// Admin live panels + Patient-facing User portal
// ============================================================

/* global apiGet, apiPost, apiPatch, authToken, API_BASE, adminNav, showToast, showPage */

function $(sel) { return document.querySelector(sel); }
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));
}
function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}
function todayISO() { return new Date().toISOString().slice(0, 10); }
function urgencyBadge(urgency) {
  const u = String(urgency || "").toLowerCase().trim();
  if (u === "high") return '<span class="badge badge-high">High</span>';
  if (u === "medium") return '<span class="badge badge-medium">Medium</span>';
  return '<span class="badge badge-low">Low</span>';
}
function statusBadge(status) {
  const s = String(status || "").toLowerCase().trim();
  if (["serving", "active", "scheduled"].includes(s)) return '<span class="badge badge-medium">' + esc(status || "Active") + "</span>";
  if (["completed"].includes(s)) return '<span class="badge badge-low">Completed</span>';
  if (["cancelled", "missed", "skipped"].includes(s)) return '<span class="badge badge-high">' + esc(status || "Closed") + "</span>";
  return '<span class="badge badge-low">' + esc(status || "Open") + "</span>";
}

async function _liveGet(endpoint) {
  if (typeof apiGet === "function") return apiGet(endpoint);
  const base = (typeof API_BASE !== "undefined" ? API_BASE : "") || "";
  const token = (typeof authToken !== "undefined" ? authToken : "") || localStorage.getItem("hq_auth_token") || "";
  const res = await fetch(base + endpoint, { headers: token ? { Authorization: "Bearer " + token } : {} });
  const json = await res.json();
  return json.data ?? json;
}

// ------------------------------------------------------------
// Admin live loaders
// ------------------------------------------------------------
async function loadDashboardStats() {
  try {
    const stats = await _liveGet("/api/dashboard/stats");
    setText("dash-total-patients", String(stats.today_appointments ?? 0));
    setText("dash-overload-count", `${stats.active_queue ?? 0} Patients`);
    setText("dash-doctors-on-duty", String(stats.doctors_on_duty ?? 0));

    // --- FIX: Only update the avg-wait stat card with the global value if NO specific
    // doctor is currently selected in the prediction widget dropdown.
    // If a doctor IS selected, we preserve the doctor-specific wait time shown there.
    const _isDoctorSelected = (() => {
      const docSel = document.getElementById("dash-doctor-select");
      if (!docSel || docSel.disabled || !docSel.value) return false;
      // dashDoctorCache is defined in the main HTML; check it safely
      const cache = (typeof dashDoctorCache !== "undefined") ? dashDoctorCache : {};
      return !!cache[String(docSel.value)];
    })();

    if (!_isDoctorSelected) {
      // No doctor selected — show hospital-wide avg wait
      setText("dash-avg-wait", `${stats.avg_wait ?? 0} min`);
      const sv = document.querySelectorAll("#sp-dashboard .stat-value");
      if (sv[1]) sv[1].textContent = (stats.avg_wait ?? 0) + " min";
    }
    // Always update the other three stat-value cards by position (safe — they don't depend on doctor)
    const sv = document.querySelectorAll("#sp-dashboard .stat-value");
    if (sv[0]) sv[0].textContent = stats.today_appointments ?? 0;
    if (sv[2]) sv[2].textContent = (stats.active_queue ?? 0) + " Patients";
    if (sv[3]) sv[3].textContent = stats.doctors_on_duty ?? 0;

    // After updating global stats, let the doctor-sync function re-assert the correct value
    if (_isDoctorSelected && typeof _dashSyncSelectedDoctorStatCard === "function") {
      _dashSyncSelectedDoctorStatCard();
    }

    // Refresh Today's Appointments donut with live dept breakdown
    try {
      const breakdown = await _liveGet("/api/dashboard/dept-breakdown");
      if (typeof renderTodayAppointmentsDonut === "function") {
        renderTodayAppointmentsDonut(breakdown, stats.today_appointments);
      }
    } catch(e) {
      if (typeof renderTodayAppointmentsDonut === "function") {
        renderTodayAppointmentsDonut([], stats.today_appointments);
      }
    }
  } catch (e) {
    console.warn("[LiveData] stats failed", e);
  }
}

async function loadDashboardQueueTable() {
  const tbody = $("#sp-dashboard table tbody");
  if (!tbody) return;
  try {
    const rows = await _liveGet("/api/dashboard/queue-summary");

    // Update the shared queueCache
    if (typeof queueCache !== "undefined" && Array.isArray(rows)) {
      rows.forEach(d => { queueCache[d.department] = d; });
    }

    // Refresh per-dept on-duty doctor counts into deptDoctorCache
    try {
      const doctors = await _liveGet("/api/doctors");
      if (typeof deptDoctorCache !== "undefined" && Array.isArray(doctors)) {
        const tmp = {};
        doctors.forEach(doc => {
          const dept = doc.department_name || doc.department || '';
          if (!dept) return;
          if (!tmp[dept]) tmp[dept] = { total: 0, onDuty: 0 };
          tmp[dept].total++;
          const st = String(doc.status || '').trim().toLowerCase();
          if (st === 'on duty' || st === 'active' || st === 'available' || st === 'on_duty') {
            tmp[dept].onDuty++;
          }
        });
        // Merge into existing cache (don't wipe departments not in this response)
        Object.assign(deptDoctorCache, tmp);
      }
    } catch(e) { /* doctor count fetch failed — use existing cache */ }

    // Delegate to the single canonical render function
    if (typeof renderQueueTable === "function") {
      renderQueueTable();
      return;
    }

    // Fallback if renderQueueTable not yet available
    const statusBadge = s => s === "High"
      ? '<span class="badge badge-high">High</span>'
      : s === "Medium"
      ? '<span class="badge badge-medium">Medium</span>'
      : '<span class="badge badge-low">Low</span>';
    tbody.innerHTML = (rows || []).map((d) => {
      const dc = (typeof deptDoctorCache !== "undefined" && deptDoctorCache[d.department]) || {};
      const onDuty = dc.onDuty != null ? dc.onDuty : "—";
      const total  = dc.total  != null ? dc.total  : "";
      const doctorLabel = dc.onDuty != null
        ? `<span style="font-weight:700;color:var(--green-700)">${onDuty}</span>`
          + (total ? `<span style="font-size:11px;color:var(--gray-400)"> / ${total} total</span>` : "")
        : `<span style="color:var(--gray-400)">—</span>`;
      return `<tr>
        <td>${esc(d.department)}</td>
        <td>${d.waiting ?? 0}</td>
        <td>${doctorLabel}</td>
        <td>${statusBadge(esc(d.status))}</td>
      </tr>`;
    }).join("");
  } catch (e) {
    console.warn("[LiveData] queue table failed", e);
  }
}

async function loadDoctorsTable() {
  const tbody = document.getElementById("doc-body");
  if (!tbody) return;
  try {
    const doctors = await _liveGet("/api/doctors");
    if (!doctors || !doctors.length) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--gray-400)">No doctors found</td></tr>';
      return;
    }
    tbody.innerHTML = doctors.map((d) => `
      <tr>
        <td>${esc(d.doctor_code ?? d.id ?? "")}</td>
        <td>${esc(d.name ?? "")}</td>
        <td>${esc(d.specialization ?? "")}</td>
        <td>${esc(d.department_name ?? "")}</td>
        <td>${esc(d.shift ?? "")}</td>
        <td>${esc(d.status ?? "")}</td>
        <td>
          <button class="btn btn-ghost btn-sm" onclick="editDoctor(${d.id})">Edit</button>
          <button class="btn btn-danger btn-sm" onclick="removeDoctor(${d.id})">Remove</button>
        </td>
      </tr>`).join("");
  } catch (e) {
    console.warn("[LiveData] doctors table failed", e);
  }
}

async function loadTodayAppointmentsTable() {
  const tbody = document.getElementById("today-appt-body");
  if (!tbody) return;
  try {
    let dateStr = todayISO();
    let appts = await _liveGet(`/api/appointments?visit_date=${encodeURIComponent(dateStr)}`);
    if (!appts || !appts.length) {
      try {
        const recent = await _liveGet("/api/appointments?per_page=1&sort=desc");
        if (recent && recent.length && recent[0].visit_date) {
          dateStr = recent[0].visit_date;
          appts = await _liveGet(`/api/appointments?visit_date=${encodeURIComponent(dateStr)}`);
        }
      } catch (_) {}
    }
    const countEl = document.getElementById("today-appt-count");
    if (!appts || !appts.length) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--gray-400);padding:24px">No appointments for today</td></tr>';
      if (countEl) countEl.textContent = "0";
      return;
    }
    if (countEl) countEl.textContent = appts.length;
    tbody.innerHTML = appts.map((a) => `
      <tr>
        <td><strong style="color:var(--green-700)">${esc(a.token_number ?? "-")}</strong></td>
        <td>${esc(a.patient_name ?? "")}</td>
        <td>${esc(a.patient_age ?? a.age ?? "")}</td>
        <td>${esc(a.department_name ?? "")}</td>
        <td>${esc(a.visit_time ?? "")}</td>
        <td>${urgencyBadge(a.urgency)}</td>
        <td>${statusBadge(a.status)}</td>
      </tr>`).join("");
  } catch (e) {
    console.warn("[LiveData] today appts failed", e);
  }
}

function activeAdminPageId() {
  const el = document.querySelector("#admin-content .sub-page.active");
  return el ? el.id : "";
}

async function refreshActivePage() {
  // Only run admin refresh when the admin page is actually visible
  const adminPage = document.getElementById("admin-page");
  if (!adminPage || adminPage.style.display === "none") return;
  const page = activeAdminPageId();
  try {
    if (page === "sp-dashboard") {
      await Promise.all([loadDashboardStats(), loadDashboardQueueTable()]);
      // Re-assert doctor-specific wait time after global stats are written
      if (typeof _dashSyncSelectedDoctorStatCard === "function") {
        _dashSyncSelectedDoctorStatCard();
      }
    }
    else if (page === "sp-doctors") await loadDoctorsTable();
    else if (page === "sp-today-appt") await loadTodayAppointmentsTable();
  } catch (e) {
    console.warn("[LiveData] refresh failed", e);
  }
}

async function refreshAllPanels() {
  await Promise.allSettled([
    loadDashboardStats(),
    loadDashboardQueueTable(),
    loadTodayAppointmentsTable(),
  ]);
  // After all panels refresh, re-assert the selected doctor's wait time
  // so concurrent global stat writes don't win the race.
  if (typeof _dashSyncSelectedDoctorStatCard === "function") {
    _dashSyncSelectedDoctorStatCard();
  }
}

function _installAdminNavHook() {
  if (typeof window.adminNav !== "function") return;
  const prev = window.adminNav;
  window.adminNav = async function(page, clickedEl) {
    const result = await Promise.resolve(prev(page, clickedEl)).catch(() => {});
    try {
      if (page === "dashboard") {
        await Promise.all([loadDashboardStats(), loadDashboardQueueTable()]);
        // Re-assert doctor-specific wait time after global stats are written
        if (typeof _dashSyncSelectedDoctorStatCard === "function") {
          _dashSyncSelectedDoctorStatCard();
        }
      }
      else if (page === "today-appt") await loadTodayAppointmentsTable();
      else if (page === "doctors") await loadDoctorsTable();
    } catch (e) {
      console.warn("[LiveData] nav hook failed", e);
    }
    return result;
  };
}

async function userBookAppointment() {
  const name = document.getElementById('ba-name')?.value.trim();
  const phone = document.getElementById('ba-phone')?.value.trim();
  const age = document.getElementById('ba-age')?.value;
  const gender = document.getElementById('ba-gender')?.value;
  const deptSel = document.getElementById('ba-dept');
  const deptId = deptSel?.value || '';
  const deptName = deptSel?.options?.[deptSel.selectedIndex]?.text || '';
  const doctorId = document.getElementById('ba-doctor')?.value || '';
  const urgency = document.getElementById('ba-urgency')?.value || 'Medium';
  const date = document.getElementById('ba-date')?.value;
  const time = document.getElementById('ba-time')?.value || '';
  const symptoms = document.getElementById('ba-symptoms')?.value.trim() || '';

  if (!name || !phone || !deptId || !date) {
    showToast('Name, phone, department and date are required', 'error');
    return;
  }

  const btn = document.querySelector('#sp-u-book-appt .btn-primary');
  const oldText = btn ? btn.textContent : '';
  if (btn) {
    btn.textContent = 'Booking…';
    btn.disabled = true;
  }

  let tokenNum = '';
  let wait = 20;
  let appt = null;

  try {
    // Reuse the logged-in user's patient_id so the booking stays linked to the same user
    let patientId = await getUserPatientId();
    if (!patientId) {
      try {
        const pt = await apiPost('/api/patients', {
          name,
          phone,
          age: parseInt(age || '0', 10) || 0,
          gender,
          notes: symptoms
        });
        patientId = pt.id;
        _cachedPatientId = String(patientId);
        localStorage.setItem('hq_user_patient_id', _cachedPatientId);
      } catch (e) {
        console.warn('patient create fallback failed', e);
      }
    }

    appt = await apiPost('/api/user/appointments', {
      name,
      phone,
      age: parseInt(age || '0', 10) || 0,
      gender,
      department_id: deptId,
      doctor_id: doctorId || null,
      visit_date: date,
      visit_time: time,
      urgency,
      symptoms
    });

    const apptData = appt.appointment || appt;
    const tokData = appt.token || {};

    tokenNum = tokData.token_number || tokData.token || ('T' + Math.floor(Math.random() * 90 + 10));
    wait = tokData.est_wait_min || 20;

    if (apptData.patient_id) {
      _cachedPatientId = String(apptData.patient_id);
      localStorage.setItem('hq_user_patient_id', _cachedPatientId);
    }

    const apptId = apptData.id || null;
    const doctorLabel = deptName || 'Any';

    // Save active token
    userActiveToken = {
      token: tokenNum,
      dept: deptName,
      wait: wait,
      position: tokData.position || (queueCache[deptName]?.waiting || 3) + 1,
      created: new Date().toISOString(),
      appointment_id: apptId
    };
    localStorage.setItem('user_active_token', JSON.stringify(userActiveToken));

    // Save to local appointment history so My Appointments can render immediately
    const historyEntry = {
      token: tokenNum,
      dept: deptName,
      doctor: doctorLabel,
      date: date,
      wait: wait + ' min',
      status: 'Scheduled',
      id: apptId || ('local_' + Date.now())
    };

    if (apptId) {
      userApptHistory = userApptHistory.filter(a => String(a.id) !== String(apptId));
    }
    userApptHistory.unshift(historyEntry);
    localStorage.setItem('user_appt_history', JSON.stringify(userApptHistory));

    // Update booking UI
    const resultBox = document.getElementById('ba-token-result');
    if (resultBox) resultBox.style.display = 'block';

    const tokenEl = document.getElementById('ba-token-num');
    const deptEl = document.getElementById('ba-token-dept');
    const waitEl = document.getElementById('ba-token-wait');

    if (tokenEl) tokenEl.textContent = tokenNum;
    if (deptEl) deptEl.textContent = deptName;
    if (waitEl) waitEl.textContent = 'Est. wait: ' + wait + ' min';

    refreshUserTokenDisplay();
    loadUserApptStats();
    renderUserMyAppts();

    // Refresh dashboard cards and if My Appointments is open, refresh it too
    if (typeof loadUserDashboard === 'function') {
      await loadUserDashboard();
    }
    if (typeof refreshMyAppointments === 'function') {
      await refreshMyAppointments();
    }

    showToast('✅ Appointment booked! Token: ' + tokenNum);

    // Keep other live panels synced
    if (typeof fetchQueueSummary === 'function') fetchQueueSummary().catch(() => {});
    if (typeof renderTokenQueue === 'function') renderTokenQueue().catch(() => {});

    // Optional: jump to My Appointments so user sees the new entry right away
    if (typeof userNav === 'function') {
      userNav('u-my-appts', document.querySelector('#user-page .nav-item[onclick*="u-my-appts"]'));
    }
  } catch (e) {
    console.error('userBookAppointment failed', e);
    showToast('Appointment booking failed: ' + (e?.message || 'Unknown error'), 'error');
  } finally {
    if (btn) {
      btn.textContent = oldText || '📅 Confirm Booking & Get Token';
      btn.disabled = false;
    }
  }
}

async function refreshUserAppointmentViewsAfterBooking() {
  renderUserMyAppts();
  refreshUserTokenDisplay();
  loadUserApptStats();
  if (typeof loadUserDashboard === 'function') {
    await loadUserDashboard();
  }
  if (typeof refreshMyAppointments === 'function') {
    await refreshMyAppointments();
  }
}

function _patchSubmitNewAppointment() {
  if (typeof window.submitNewAppointment !== "function") return;
  const orig = window.submitNewAppointment;
  window.submitNewAppointment = async function(...args) {
    const result = await orig.apply(this, args);
    setTimeout(refreshAllPanels, 400);
    return result;
  };
}

function _patchStaffCreateAppointment() {
  if (typeof window.staffCreateAppointment !== "function") return;
  const orig = window.staffCreateAppointment;
  window.staffCreateAppointment = async function(...args) {
    const result = await orig.apply(this, args);
    setTimeout(refreshAllPanels, 400);
    return result;
  };
}

// ------------------------------------------------------------
// Patient portal
// ------------------------------------------------------------
const PatientPortal = {
  activePage: "dashboard",
  stream: null,
  streamRetry: null,
  streamRefresh: null,
  settings: loadPatientSettings(),
  data: {
    profile: null,
    dashboard: null,
    appointments: [],
    queue: null,
    notifications: [],
    departments: [],
    doctors: [],
  },

  isPatientUser() {
    const user = JSON.parse(localStorage.getItem("hq_user") || "null");
    // Allow any logged-in non-admin user to use patient portal
    return !!(user && (user.patient_id || (user.role && user.role !== "admin")));
  },

  async boot(showUserPage = false) {
    if (!this.isPatientUser()) return;
    // Restore saved auth token on page refresh so API calls authenticate immediately
    if ((typeof authToken === "undefined" || !authToken) && typeof window !== "undefined") {
      const saved = localStorage.getItem("hq_auth_token");
      if (saved) {
        authToken = saved;
        if (typeof window !== "undefined") window.authToken = saved;
      }
    }
    this.installStyles();
    this.installShell();
    if (showUserPage && typeof showPage === "function") showPage("user");
    await this.refreshCore();
    await this.navigate(this.activePage || "dashboard");
    this.connectStream();
  },

  installStyles() {
    if (document.getElementById("patient-portal-style")) return;
    const style = document.createElement("style");
    style.id = "patient-portal-style";
    style.textContent = `
      .patient-grid{display:grid;grid-template-columns:1.35fr 1fr;gap:18px;margin-bottom:18px}
      .patient-grid-3{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}
      .patient-hero{background:linear-gradient(135deg,var(--green-600),var(--green-500));color:#fff;border-radius:18px;padding:24px;box-shadow:var(--shadow)}
      .patient-hero .tiny{font-size:11px;opacity:.78;text-transform:uppercase;letter-spacing:1px}
      .patient-progress{height:10px;background:rgba(255,255,255,.18);border-radius:999px;overflow:hidden;margin-top:14px}
      .patient-progress > span{display:block;height:100%;background:#fff;border-radius:999px}
      .patient-list{display:grid;gap:12px}
      .patient-item{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;padding:14px 0;border-bottom:1px solid var(--gray-100)}
      .patient-item:last-child{border-bottom:none}
      .patient-meta{font-size:12px;color:var(--gray-400);margin-top:4px}
      .patient-nav-note{font-size:10px;color:var(--gray-500);padding:0 20px;margin:4px 0 10px}
      .patient-card-stack{display:grid;gap:18px}
      .patient-empty{padding:30px 18px;text-align:center;color:var(--gray-400)}
      .patient-form-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}
      .patient-form-grid .full{grid-column:1/-1}
      .patient-pill{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;background:var(--green-100);color:var(--green-700);font-size:12px;font-weight:700}
      .patient-dept-card{padding:18px;border:1px solid var(--gray-100);border-radius:14px;background:linear-gradient(180deg,#fff,var(--green-50))}
      .patient-doctor-card{padding:18px;border:1px solid var(--gray-100);border-radius:14px;background:#fff}
      .patient-notice{padding:14px 16px;border-radius:12px;border:1px solid var(--gray-100);background:#fff}
      .patient-notice.high{background:#fff5f5;border-color:#fecaca}
      .patient-notice.medium{background:#fffbeb;border-color:#fde68a}
      .patient-settings-line{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:12px 0;border-bottom:1px solid var(--gray-100)}
      .patient-settings-line:last-child{border-bottom:none}
      .patient-toggle{width:44px;height:24px;border-radius:999px;background:var(--gray-300);position:relative;cursor:pointer;transition:.2s}
      .patient-toggle::after{content:'';position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#fff;transition:.2s}
      .patient-toggle.on{background:var(--green-500)}
      .patient-toggle.on::after{left:23px}
      .patient-top-actions{display:flex;gap:10px;flex-wrap:wrap}
      @media (max-width: 980px){
        .patient-grid,.patient-grid-3,.patient-form-grid{grid-template-columns:1fr}
        .sidebar{position:relative;height:auto;min-height:auto}
        .topbar{padding:0 18px}
        .content{padding:18px}
      }
    `;
    document.head.appendChild(style);
  },

  installShell() {
    const page = document.getElementById("user-page");
    if (!page) return;
    page.innerHTML = `
      <div class="layout" id="patient-shell">
        <nav class="sidebar">
          <div class="sidebar-brand">
            <div class="brand-icon">
              <svg viewBox="0 0 24 24"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"></path></svg>
            </div>
            <div class="brand-text">
              <h2>Hospital Queue</h2>
              <span>User Portal</span>
            </div>
          </div>
          <div class="sidebar-nav">
            <div class="nav-section-label">Account</div>
            <div class="nav-item active" data-page="dashboard" onclick="PatientPortal.navigate('dashboard', this)">Dashboard</div>
            <div class="nav-section-label">Appointments</div>
            <div class="nav-item" data-page="book" onclick="PatientPortal.navigate('book', this)">Take Token / Book</div>
            <div class="nav-item" data-page="appointments" onclick="PatientPortal.navigate('appointments', this)">My Appointments</div>
            <div class="nav-item" data-page="queue" onclick="PatientPortal.navigate('queue', this)">Live Queue Status</div>
            <div class="nav-item" data-page="notifications" onclick="PatientPortal.navigate('notifications', this)">Notifications</div>
            <div class="nav-section-label">Hospital</div>
            <div class="nav-item" data-page="departments" onclick="PatientPortal.navigate('departments', this)">Departments</div>
            <div class="nav-item" data-page="doctors" onclick="PatientPortal.navigate('doctors', this)">Doctors</div>
            <div class="nav-item" data-page="schedule" onclick="PatientPortal.navigate('schedule', this)">Schedule &amp; Contact</div>
            <div class="nav-item" data-page="settings" onclick="PatientPortal.navigate('settings', this)">Settings</div>
            <div class="patient-nav-note">Live queue updates sync with the Admin view automatically.</div>
          </div>
          <div class="sidebar-footer">
            <button class="logout-btn" onclick="doLogout()">
              <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9"></path></svg>
              Logout
            </button>
          </div>
        </nav>
        <div class="main">
          <div class="topbar">
            <div class="topbar-title" id="patient-page-title">Dashboard</div>
            <div class="topbar-right">
              <button class="notif-btn" onclick="PatientPortal.navigate('notifications')">
                <svg viewBox="0 0 24 24"><path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9M13.73 21a2 2 0 01-3.46 0"></path></svg>
                <span class="notif-badge" id="patient-notif-badge" style="display:none">0</span>
              </button>
              <div class="user-badge">
                <div class="user-avatar" id="patient-avatar">U</div>
                <div class="user-info">
                  <div class="user-name" id="patient-user-name">User</div>
                  <div class="user-role" id="patient-user-role">Patient</div>
                </div>
              </div>
            </div>
          </div>
          <div class="content" id="patient-content"></div>
        </div>
      </div>`;
  },

  async refreshCore() {
    // Upgrade demo token to real session and sync any pending offline bookings
    await this._ensureRealToken();
    await this._syncOfflineBookings();
    const results = await Promise.allSettled([
      apiGet("/api/user/profile"),        // 0
      apiGet("/api/user/dashboard"),       // 1
      apiGet("/api/user/appointments"),    // 2
      apiGet("/api/user/queue-status"),    // 3
      apiGet("/api/user/notifications"),   // 4
      apiGet("/api/departments"),          // 5
      apiGet("/api/doctors"),              // 6
      apiGet("/api/dashboard/queue-summary"), // 7
      apiGet("/api/schedules"),            // 8  doctor schedules (for Schedule & Contact page)
      apiGet("/api/settings"),             // 9  hospital settings (contact info, shift timings)
    ]);
    const [profile, dashboard, appointments, queue, notifications, departments, doctors, queueSummary, schedules, siteSettings] = results;
    if (profile.status === "fulfilled") this.data.profile = profile.value;
    if (dashboard.status === "fulfilled") {
      this.data.dashboard = dashboard.value;
      // Attach queue summary to dashboard data for renderDashboard
      if (queueSummary.status === "fulfilled" && this.data.dashboard) {
        this.data.dashboard.queue_summary = Array.isArray(queueSummary.value) ? queueSummary.value : [];
      }
    }
    if (appointments.status === "fulfilled") {
      const fetched = Array.isArray(appointments.value) ? appointments.value : [];
      // Only replace if the API actually returned data; keep existing if empty (avoids blank-on-refresh)
      if (fetched.length > 0 || !this.data.appointments?.length) {
        this.data.appointments = fetched;
      }
    }
    if (queue.status === "fulfilled") this.data.queue = queue.value;
    if (notifications.status === "fulfilled") this.data.notifications = Array.isArray(notifications.value) ? notifications.value : [];
    this.data.adminAlerts = []; // user role cannot access /api/alerts; notifications covers all alerts
    if (departments.status === "fulfilled") this.data.departments = Array.isArray(departments.value) ? departments.value : [];
    if (doctors.status === "fulfilled") this.data.doctors = Array.isArray(doctors.value) ? doctors.value : [];
    if (schedules.status === "fulfilled") this.data.schedules = Array.isArray(schedules.value) ? schedules.value : [];
    if (siteSettings.status === "fulfilled") this.data.siteSettings = (siteSettings.value && typeof siteSettings.value === "object") ? siteSettings.value : {};
    // Merge offline/localStorage appointments so locally-registered users always see their bookings
    this._mergeLocalAppointments();
    this.paintUserHeader();
  },

  // ── Merge localStorage appointments into this.data.appointments ───────────
  // Covers: user_appt_history, user_active_tokens, hq_pending_bookings
  // Deduplicates by token_number+department so API data takes priority.
  _mergeLocalAppointments() {
    const apiAppts = this.data.appointments || [];
    // Build a set of identifiers already present from API
    const apiKeys = new Set(apiAppts.map(a =>
      [a.token_number, a.department_name, a.visit_date].filter(Boolean).join("|")
    ));

    const localAppts = [];

    // 1. Active tokens (most recent bookings made offline)
    try {
      const tokens = JSON.parse(localStorage.getItem("user_active_tokens") || "[]");
      tokens.forEach(t => {
        if (!t) return;
        const key = [t.token_number || t.token, t.department_name || t.dept, t.visit_date || t.date].filter(Boolean).join("|");
        if (!apiKeys.has(key)) {
          localAppts.push({
            id: t.id || `local_${t.token_number || t.token}`,
            visit_date: t.visit_date || t.date || new Date().toLocaleDateString("en-CA"),
            department_name: t.department_name || t.dept || "—",
            doctor_name: t.doctor_name || t.doctor || "—",
            token_number: t.token_number || t.token || "—",
            token_est_wait_min: t.est_wait_min || t.token_est_wait_min || 0,
            status: t.status || "Scheduled",
            queue_status: t.queue_status || "Waiting",
            _source: "local_token",
          });
          apiKeys.add(key);
        }
      });
    } catch(e) {}

    // 2. Appointment history saved offline
    try {
      const hist = JSON.parse(localStorage.getItem("user_appt_history") || "[]");
      hist.forEach(a => {
        if (!a) return;
        const key = [a.token || a.token_number, a.department_name || a.dept, a.visit_date || a.date].filter(Boolean).join("|");
        if (!apiKeys.has(key)) {
          localAppts.push({
            id: a.id || `local_hist_${key}`,
            visit_date: a.visit_date || a.date || "—",
            department_name: a.department_name || a.dept || "—",
            doctor_name: a.doctor_name || a.doctor || "—",
            token_number: a.token_number || a.token || "—",
            token_est_wait_min: a.est_wait_min || 0,
            status: a.status || "Scheduled",
            queue_status: a.queue_status || a.status || "Scheduled",
            _source: "local_hist",
          });
          apiKeys.add(key);
        }
      });
    } catch(e) {}

    // 3. Pending (not-yet-synced) bookings
    try {
      const pending = JSON.parse(localStorage.getItem("hq_pending_bookings") || "[]");
      pending.forEach(item => {
        const p = item.payload || item;
        const key = [p.token_number, p.department_name, p.visit_date].filter(Boolean).join("|");
        if (!apiKeys.has(key)) {
          localAppts.push({
            id: `pending_${key}`,
            visit_date: p.visit_date || new Date().toLocaleDateString("en-CA"),
            department_name: p.department_name || "—",
            doctor_name: p.doctor_name || "—",
            token_number: p.token_number || "Pending",
            token_est_wait_min: p.est_wait_min || 0,
            status: "Scheduled",
            queue_status: "Waiting",
            _source: "pending",
          });
          apiKeys.add(key);
        }
      });
    } catch(e) {}

    if (localAppts.length) {
      // API appointments take priority; local ones appended after
      this.data.appointments = [...apiAppts, ...localAppts];
    }
  },

  paintUserHeader() {
    const profile = this.data.profile;
    if (!profile) return;
    const name = profile.name || profile.display_name || "User";
    const avatar = document.getElementById("patient-avatar");
    const userName = document.getElementById("patient-user-name");
    if (avatar) avatar.textContent = name.charAt(0).toUpperCase();
    if (userName) userName.textContent = name;
    // Recompute badge with token + dept + backend alerts
    this._updateNotifBadge();
    // Fire toast for newly-eligible token alerts
    this.checkAndPushUserTokenAlerts();
  },

  async navigate(page, clickedEl) {
    this.activePage = page;
    document.querySelectorAll("#patient-shell .nav-item").forEach((el) => el.classList.remove("active"));
    const target = clickedEl || document.querySelector(`#patient-shell .nav-item[data-page="${page}"]`);
    if (target) target.classList.add("active");
    const titleMap = {
      dashboard: "Dashboard",
      book: "Take Token / Book Appointment",
      appointments: "My Tokens / Appointments",
      queue: "Live Queue Status",
      notifications: "Notifications / Alerts",
      departments: "Departments",
      doctors: "Doctors",
      schedule: "Schedule & Contact",
      settings: "Settings",
    };
    setText("patient-page-title", titleMap[page] || "User Portal");

    if (page === "dashboard") this.renderDashboard();
    else if (page === "book") this.renderBooking();
    else if (page === "appointments") {
      // Always fetch fresh appointments when navigating to this page
      try {
        const fresh = await apiGet("/api/user/appointments").catch(() => null);
        if (fresh && Array.isArray(fresh) && fresh.length > 0) {
          this.data.appointments = fresh;
        }
        this._mergeLocalAppointments();
      } catch(_) {}
      this.renderAppointments();
    }
    else if (page === "queue") this.renderQueue();
    else if (page === "notifications") this.renderNotifications();
    else if (page === "departments") this.renderDepartments();
    else if (page === "doctors") this.renderDoctors();
    else if (page === "schedule") this.renderSchedule();
    else if (page === "settings") this.renderSettings();
  },


  
  renderDashboard() {
    const content = document.getElementById("patient-content");
    const d = this.data.dashboard || {};
    // Map fields from /api/user/dashboard: active_token, total_appointments, completed_appointments, avg_wait_min
    const token = d.active_token || d.current_token || {};
    const totalAppts = d.total_appointments || 0;
    const completedAppts = d.completed_appointments || 0;
    const avgWait = d.avg_wait_min || 0;
    // Show the user's own token estimated wait when an active token exists;
    // fall back to hospital-wide average only when there is no active token.
    const hasActiveToken = !!(token && token.token_number);
    const displayWait = hasActiveToken ? (token.est_wait_min ?? avgWait) : avgWait;
    const waitLabel = hasActiveToken ? "Your est. wait" : "Hospital-wide";
    const todayForDash = new Date().toLocaleDateString("en-CA");
    const recent = (this.data.appointments || [])
      .filter(a => {
        const activeStatus = ["active","scheduled","serving","waiting"].includes(String(a.status||"").toLowerCase());
        const visitDate = a.visit_date || a.date || "";
        // Show in history if: non-active status OR date has passed
        return !activeStatus || (visitDate && visitDate < todayForDash);
      })
      .slice(0, 5);
    const notifCount = (this.data.notifications || []).length;
    const profile = this.data.profile || {};
    const userName = profile.display_name || profile.name || profile.username || "Patient";
    // Build queue summary from departments data if not in dashboard
    const queueSummary = d.queue_summary || [];
    const progressPct = 100;
    content.innerHTML = `
      <div class="section-header">
        <h2>Welcome back, ${esc(userName)}</h2>
        <p>Track your token, book appointments, and follow live queue progress in real time.</p>
      </div>
      <div class="stats-row">
        <div class="stat-card"><div class="stat-icon green"><svg stroke-width="2" viewBox="0 0 24 24" fill="none" stroke="var(--green-600)"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg></div><div><div class="stat-label">My Appointments</div><div class="stat-value">${totalAppts}</div><div class="stat-change neutral">${completedAppts} completed</div></div></div>
        <div class="stat-card"><div class="stat-icon green"><svg stroke-width="2" viewBox="0 0 24 24" fill="none" stroke="var(--green-600)"><path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2"/></svg></div><div><div class="stat-label">Active Token</div><div class="stat-value">${esc(token.token_number || "—")}</div><div class="stat-change neutral">${esc(token.department_name || "No active token")}</div></div></div>
        <div class="stat-card"><div class="stat-icon orange"><svg stroke-width="2" viewBox="0 0 24 24" fill="none" stroke="#ea580c"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg></div><div><div class="stat-label">Avg Wait Time</div><div class="stat-value">${displayWait} min</div><div class="stat-change neutral">${waitLabel}</div></div></div>
        <div class="stat-card"><div class="stat-icon purple"><svg stroke-width="2" viewBox="0 0 24 24" fill="none" stroke="#7c3aed"><path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9M13.73 21a2 2 0 01-3.46 0"/></svg></div><div><div class="stat-label">Notifications</div><div class="stat-value">${notifCount}</div><div class="stat-change neutral">Active alerts</div></div></div>
      </div>
      <div class="patient-grid">
        <div class="patient-card-stack">
          <div class="patient-hero">
            <div class="tiny">Current Token Display</div>
            <div class="token-num-big">${esc(token.token_number || "—")}</div>
            <div class="token-dept">${esc(token.department_name || "No queue assigned yet")}</div>
            <div style="margin-top:10px;font-size:13px;opacity:.85">
              Status: ${esc(token.status || "Waiting")} • Estimated wait: ${token.est_wait_min || 0} min
            </div>
            <div class="patient-progress"><span style="width:${progressPct}%"></span></div>
            <div style="margin-top:10px;font-size:12px;opacity:.8">
              ${token.token_number ? "Token is active — check Live Queue for details" : "No active token — book an appointment to join the queue"}
            </div>
          </div>
          <div class="card">
            <div class="card-title">Appointment History</div>
            ${recent.length ? `
              <table>
                <thead><tr><th>Date</th><th>Department</th><th>Token</th><th>Status</th></tr></thead>
                <tbody>
                  ${recent.map((a) => `
                    <tr>
                      <td>${esc(a.visit_date || "")}</td>
                      <td>${esc(a.department_name || "")}</td>
                      <td>${esc(a.token_number || "—")}</td>
                      <td>${statusBadge(a.status)}</td>
                    </tr>`).join("")}
                </tbody>
              </table>` : '<div class="patient-empty">No appointments yet.</div>'}
          </div>
        </div>
        <div class="patient-card-stack">
          <div class="card">
            <div class="card-title">Quick Actions</div>
            <div class="patient-top-actions">
              <button class="btn btn-primary" onclick="PatientPortal.navigate('book')">Take Token</button>
              <button class="btn btn-outline" onclick="PatientPortal.navigate('appointments')">My Appointments</button>
              <button class="btn btn-ghost" onclick="PatientPortal.navigate('queue')">Live Queue</button>
            </div>
          </div>
          <div class="card">
            <div class="card-title">Live Queue Snapshot</div>
            ${queueSummary.length ? `
              <table>
                <thead><tr><th>Department</th><th>Waiting</th><th>Avg Wait</th><th>Status</th></tr></thead>
                <tbody>${queueSummary.slice(0, 6).map((row) => `
                  <tr>
                    <td>${esc(row.department)}</td>
                    <td>${row.waiting ?? 0}</td>
                    <td>${row.avg_wait_min ?? 0} min</td>
                    <td>${statusBadge(row.status)}</td>
                  </tr>`).join("")}</tbody>
              </table>` : '<div class="patient-empty">Queue data will appear here.</div>'}
          </div>
        </div>
      </div>`;
  },

  renderBooking() {
    const content = document.getElementById("patient-content");
    const depts = this.data.departments || [];
    const profile = this.data.profile || {};
    const docs = this.filteredDoctors();
    const selectedDoctorId = this.bookingFormValue("book-doctor", "");
    const selectedDate = this.bookingFormValue("book-date", todayISO());
    const selectedUrgency = this.bookingFormValue("book-urgency", "Medium");
    const selectedPhone = this.bookingFormValue("book-phone", this.data.profile?.patient_phone || this.data.profile?.account_phone || "");
    const selectedSymptoms = this.bookingFormValue("book-symptoms", "");
    const selectedTime = this.selectedBookingTime();
    const timeSlots = this.bookingTimeSlots(docs, selectedDoctorId, selectedTime);
    const activeToken = this.data.dashboard?.active_token || {};
    const queueSummary = (this.data.queue && this.data.queue.queue_summary) || (this.data.dashboard && this.data.dashboard.queue_summary) || [];
    content.innerHTML = `
      <div class="section-header"><h2>Take Token / Book Appointment</h2><p>Register patient details and generate a shared queue token instantly.</p></div>
      <div class="grid-23">
        <div class="card">
          <div class="pred-form" id="book-form">
            <div class="form-group-inline"><label class="form-label-sm">Patient Name *</label><input class="form-input-sm" id="book-name" placeholder="Full name" value="${esc(profile.name || profile.display_name || "")}"></div>
            <div class="form-group-inline"><label class="form-label-sm">Phone *</label><input class="form-input-sm" id="book-phone" placeholder="Mobile number" type="tel" value="${esc(selectedPhone)}"></div>
            <div class="form-group-inline"><label class="form-label-sm">Age</label><input class="form-input-sm" id="book-age" max="120" min="0" placeholder="e.g. 35" type="number" value="${esc(profile.age || "")}"></div>
            <div class="form-group-inline"><label class="form-label-sm">Gender</label><select class="form-select" id="book-gender"><option ${String(profile.gender || "").toLowerCase() === "male" ? "selected" : ""}>Male</option><option ${String(profile.gender || "").toLowerCase() === "female" ? "selected" : ""}>Female</option><option ${!["male", "female"].includes(String(profile.gender || "").toLowerCase()) ? "selected" : ""}>Other</option></select></div>
            <div class="form-group-inline"><label class="form-label-sm">Department *</label>
              <select class="form-select" id="book-department" onchange="PatientPortal.renderBooking()">
                ${depts.map((d) => `<option value="${d.id}" ${String(PatientPortal.selectedDepartmentId()) === String(d.id) ? "selected" : ""}>${esc(d.name)}</option>`).join("")}
              </select>
            </div>
            <div class="form-group-inline"><label class="form-label-sm">Doctor</label>
              <select class="form-select" id="book-doctor" onchange="PatientPortal.renderBooking()">
                <option value="">Any available doctor</option>
                ${docs.map((doc) => `<option value="${doc.id}" ${String(selectedDoctorId) === String(doc.id) ? "selected" : ""}>${esc(doc.name)} • ${esc(doc.shift || "")} Shift (${esc(doc.status || "")})</option>`).join("")}
              </select>
            </div>
            <div class="form-group-inline"><label class="form-label-sm">Urgency Level *</label>
              <select class="form-select" id="book-urgency">
                <option ${selectedUrgency === "Low" ? "selected" : ""}>Low</option><option ${selectedUrgency === "Medium" ? "selected" : ""}>Medium</option><option ${selectedUrgency === "High" ? "selected" : ""}>High</option>
              </select>
            </div>
            <div class="form-group-inline"><label class="form-label-sm">Visit Date</label><input class="form-input-sm" id="book-date" type="date" value="${esc(selectedDate)}"></div>
            <div class="form-group-inline"><label class="form-label-sm">Visit Time</label><select class="form-select" id="book-time">${timeSlots.options}</select><div id="book-time-hint" style="${timeSlots.hint ? "" : "display:none;"}margin-top:6px;font-size:11px;color:var(--gray-400)">${esc(timeSlots.hint || "")}</div></div>
            <div class="form-group-inline form-row"><label class="form-label-sm">Symptoms / Notes</label><textarea class="form-input-sm" id="book-symptoms" rows="3" placeholder="Describe symptoms...">${esc(selectedSymptoms)}</textarea></div>
          </div>
          <div style="display:flex;gap:10px;margin-top:8px">
            <button class="btn btn-primary btn-lg" onclick="PatientPortal.submitBooking()" style="flex:1">Register &amp; Generate Token</button>
            <button class="btn btn-ghost" onclick="PatientPortal.renderBooking()">Reset</button>
          </div>
        </div>
        <div>
          <div class="card mb18" id="book-result" style="${activeToken.token_number ? "" : "display:none"}">
            ${this.bookingResultHtml(activeToken, { department_name: activeToken.department_name })}
            <div style="margin-top:14px;display:flex;gap:10px">
              <button class="btn btn-outline" onclick="PatientPortal.navigate('queue')" style="flex:1">View Live Queue</button>
              <button class="btn btn-ghost" onclick="PatientPortal.renderBooking()" style="flex:1">New Booking</button>
            </div>
          </div>
          <div class="card">
            <div class="card-title">Queue Status</div>
            ${queueSummary.length ? `<table><thead><tr><th>Department</th><th>Waiting</th><th>Status</th></tr></thead><tbody>${queueSummary.map((row) => `
              <tr><td>${esc(row.department)}</td><td>${row.waiting ?? 0}</td><td>${statusBadge(row.status)}</td></tr>`).join("")}</tbody></table>` : '<div class="patient-empty">Queue summary unavailable.</div>'}
          </div>
        </div>
      </div>`;
  },

  renderAppointments() {
    const content = document.getElementById("patient-content");
    const rows = this.data.appointments || [];
    // Today's date in local YYYY-MM-DD (no time zone shift)
    const todayStr = new Date().toLocaleDateString("en-CA"); // "YYYY-MM-DD"
    const isActiveAppt = (a) => {
      const activeStatus = ["active", "scheduled", "serving", "waiting"].includes(
        String(a.status || "").toLowerCase()
      );
      if (!activeStatus) return false;
      // If the appointment date is strictly before today, move it to history
      const visitDate = a.visit_date || a.date || "";
      if (visitDate && visitDate < todayStr) return false;
      return true;
    };
    const active = rows.filter((a) => isActiveAppt(a));
    const history = rows.filter((a) => !isActiveAppt(a));
    content.innerHTML = `
      <div class="section-header"><h2>My Tokens / Appointments</h2><p>View active bookings, queue tokens, and your appointment history.</p></div>
      <div class="card mb18">
        <div class="card-title">Active Bookings</div>
        ${active.length ? `
          <table>
            <thead><tr><th>Date</th><th>Department</th><th>Doctor</th><th>Token</th><th>Wait</th><th>Status</th></tr></thead>
            <tbody>${active.map((a) => `
              <tr>
                <td>${esc(a.visit_date || "")}</td>
                <td>${esc(a.department_name || "")}</td>
                <td>${esc(a.doctor_name || "Any")}</td>
                <td>${esc(a.token_number || "Pending")}</td>
                <td>${a.token_est_wait_min || 0} min</td>
                <td>${statusBadge(a.status)}</td>
              </tr>`).join("")}</tbody>
          </table>` : '<div class="patient-empty">No active appointments.</div>'}
      </div>
      <div class="card">
        <div class="card-title">Appointment History</div>
        ${history.length ? `
          <table>
            <thead><tr><th>Date</th><th>Department</th><th>Token</th><th>Doctor</th></tr></thead>
            <tbody>${history.map((a) => `
              <tr>
                <td>${esc(a.visit_date || "")}</td>
                <td>${esc(a.department_name || "")}</td>
                <td>${esc(a.token_number || "—")}</td>
                <td>${esc(a.doctor_name || "—")}</td>
              </tr>`).join("")}</tbody>
          </table>` : '<div class="patient-empty">No appointment history yet.</div>'}
      </div>`;
  },

  renderQueue() {
    const content = document.getElementById("patient-content");
    const queue = this.data.queue || {};
    const token = queue.active_token || {};
    const liveQueue = Array.isArray(queue.live_queue) ? queue.live_queue : [];
    const summary = queue.queue_summary || [];
    const search = this.queueFilterValue("uq-search").toLowerCase().trim();
    const deptFilter = this.queueFilterValue("uq-dept-filter").trim();
    const doctorFilter = this.queueFilterValue("uq-doctor-filter").trim();
    const urgencyFilter = this.queueFilterValue("uq-urgency-filter").trim().toLowerCase();
    const departments = [...new Set(liveQueue.map((row) => String(row.department_name || "").trim()).filter(Boolean))].sort();
    const doctors = [...new Set(liveQueue.map((row) => String(row.doctor_name || "Any available doctor").trim()).filter(Boolean))].sort();
    const filteredQueue = liveQueue.filter((row) => {
      const deptName = String(row.department_name || "").trim();
      const doctorName = String(row.doctor_name || "Any available doctor").trim();
      const urgency = String(row.urgency || "").trim().toLowerCase();
      const searchText = [row.token_number, row.patient_name, row.doctor_name, row.department_name, row.status].join(" ").toLowerCase();
      if (deptFilter && deptName !== deptFilter) return false;
      if (doctorFilter && doctorName !== doctorFilter) return false;
      if (urgencyFilter && urgency !== urgencyFilter) return false;
      if (search && !searchText.includes(search)) return false;
      return true;
    });
    const ahead = token.status === "Waiting"
      ? liveQueue.filter((row) => row.department_id === token.department_id && row.status === "Waiting" && row.position < token.position).length
      : 0;
    content.innerHTML = `
      <div class="section-header"><h2>Live Queue Status</h2><p>View the same hospital-wide live queue that the Admin Token Queue screen shows.</p></div>
      <div class="card mb18">
        <div class="card-title">Live Queue</div>
        <div class="filter-bar tq-filter-bar">
          <select class="form-select" id="uq-dept-filter" onchange="PatientPortal.renderQueue()">
            <option value="">All Departments</option>
            ${departments.map((name) => `<option value="${esc(name)}" ${deptFilter === name ? "selected" : ""}>${esc(name)}</option>`).join("")}
          </select>
          <select class="form-select" id="uq-doctor-filter" onchange="PatientPortal.renderQueue()">
            <option value="">All Doctors</option>
            ${doctors.map((name) => `<option value="${esc(name)}" ${doctorFilter === name ? "selected" : ""}>${esc(name)}</option>`).join("")}
          </select>
          <select class="form-select" id="uq-urgency-filter" onchange="PatientPortal.renderQueue()">
            <option value="">All Urgency Levels</option>
            <option value="High" ${urgencyFilter === "high" ? "selected" : ""}>High</option>
            <option value="Medium" ${urgencyFilter === "medium" ? "selected" : ""}>Medium</option>
            <option value="Low" ${urgencyFilter === "low" ? "selected" : ""}>Low</option>
          </select>
          <span class="filter-clear" onclick="PatientPortal.resetQueueFilters()" style="margin-left:auto">Clear Filters</span>
        </div>
        <div class="search-box">
          <svg stroke-width="2" viewbox="0 0 24 24"><circle cx="11" cy="11" r="8"></circle><path d="m21 21-4.35-4.35"></path></svg>
          <input id="uq-search" oninput="PatientPortal.renderQueue()" placeholder="Search by patient, token, or doctor..." value="${esc(this.queueFilterValue("uq-search"))}"/>
        </div>
        ${filteredQueue.length ? `
          <table>
            <thead><tr><th>Token</th><th>Patient</th><th>Doctor</th><th>Dept</th><th>Wait</th><th>Urgency</th></tr></thead>
            <tbody>${filteredQueue.map((row) => `
              <tr>
                <td><strong style="color:var(--green-700)">${esc(row.token_number || "-")}</strong></td>
                <td>
                  <div style="font-weight:700">${esc(row.patient_name || "Patient")}</div>
                  <div class="patient-meta">${esc(row.status || "Waiting")}</div>
                </td>
                <td>${esc(row.doctor_name || "Any available doctor")}</td>
                <td>${esc(row.department_name || "")}</td>
                <td>${row.status === "Serving" ? "Now" : "~" + (row.est_wait_min || 0) + " min"}</td>
                <td>${urgencyBadge(row.urgency)}</td>
              </tr>`).join("")}</tbody>
          </table>` : '<div class="patient-empty" style="padding:32px">No patients in queue</div>'}
      </div>
      <div class="grid-23">
        <div class="card">
          <div class="patient-hero">
            <div class="tiny">Current Token</div>
            <div class="token-num-big">${esc(token.token_number || "—")}</div>
            <div class="token-dept">${esc(token.department_name || "Not in queue")}</div>
            <div style="margin-top:12px;font-size:13px;opacity:.9">Estimated wait: ${token.est_wait_min || 0} min</div>
            <div style="margin-top:6px;font-size:13px;opacity:.9">People ahead: ${ahead}</div>
          </div>
        </div>
        <div class="card">
          <div class="card-title">Hospital Queue Summary</div>
          ${summary.length ? `<table><thead><tr><th>Department</th><th>Waiting</th><th>Status</th></tr></thead><tbody>${summary.map((row) => `
            <tr><td>${esc(row.department)}</td><td>${row.waiting ?? 0}</td><td>${statusBadge(row.status)}</td></tr>`).join("")}</tbody></table>` : '<div class="patient-empty">Queue summary unavailable.</div>'}
        </div>
      </div>`;
  },

  // ── Token alert tracking: remember which tokens we already notified ──────
  _notifiedTokens: new Set(),

  // ── Build user-specific token alerts (wait ≤ 20 min) ─────────────────────
  buildUserTokenAlerts() {
    const tokenAlerts = [];
    // Pull from live appointments array (refreshCore fetches /api/user/appointments)
    const appts = this.data.appointments || [];
    appts.forEach(a => {
      const status = String(a.queue_status || a.status || "").toLowerCase();
      // Only for active/waiting tokens
      if (!["waiting", "active", "scheduled", "serving"].includes(status)) return;
      const tokenNum = a.token_number || "";
      if (!tokenNum || tokenNum === "—") return;
      // Estimate wait: prefer token-level wait, then appointment-level
      const waitMin = a.token_est_wait_min != null ? Number(a.token_est_wait_min)
        : a.est_wait_min != null ? Number(a.est_wait_min) : null;
      if (waitMin === null) return;

      if (waitMin <= 20) {
        const deptName = a.department_name || "Your Department";
        const urgency = waitMin <= 5 ? "high" : "medium";
        const icon = waitMin <= 5 ? "🚨" : "⏰";
        const label = waitMin <= 5
          ? `Token ${tokenNum} — You're next! Please proceed to ${deptName}.`
          : `Token ${tokenNum} — Only ~${waitMin} min wait in ${deptName}. Please be ready.`;
        tokenAlerts.push({
          level: urgency,
          type: "token",
          dept: deptName,
          tokenNum,
          waitMin,
          message: label,
          icon,
          key: `token_${tokenNum}_${deptName}`,
        });
      }
    });
    return tokenAlerts;
  },

  // ── Push toast + badge for new token alerts ───────────────────────────────
  checkAndPushUserTokenAlerts() {
    const tokenAlerts = this.buildUserTokenAlerts();
    tokenAlerts.forEach(a => {
      if (!this._notifiedTokens.has(a.key)) {
        this._notifiedTokens.add(a.key);
        safeToast(`🔔 ${a.message}`, "info");
      }
    });
    // Update badge count with total unread
    this._updateNotifBadge();
  },

  // ── Update the bell badge count ──────────────────────────────────────────
  _updateNotifBadge() {
    const tokenAlerts = this.buildUserTokenAlerts();
    const deptAlerts = this._buildDeptAlerts();
    const backendNotifs = (this.data.notifications || []).filter(n => {
      const lvl = String(n.level || "").toLowerCase();
      return lvl === "high" || lvl === "critical" || lvl === "medium";
    });
    const total = tokenAlerts.length + deptAlerts.length + backendNotifs.length;
    const badge = document.getElementById("patient-notif-badge");
    if (badge) {
      badge.textContent = String(total);
      badge.style.display = total > 0 ? "inline-flex" : "none";
    }
  },

  // ── Build dept-level high-wait alerts ────────────────────────────────────
  _buildDeptAlerts() {
    const queueSummary = (this.data.dashboard && this.data.dashboard.queue_summary) || [];
    const alerts = [];
    queueSummary.forEach((dept) => {
      const wait = dept.avg_wait_min || 0;
      const waiting = dept.waiting || 0;
      if (wait >= 60) {
        alerts.push({
          level: wait >= 90 ? "high" : "medium",
          type: "dept",
          dept: dept.department,
          message: `${dept.department} average wait is ${wait} min — ${waiting} patients currently waiting. Consider visiting later or choosing another department.`,
          icon: wait >= 90 ? "🔴" : "🟡",
          key: `dept_${dept.department}_${wait}`,
        });
      } else if (dept.status === "High") {
        alerts.push({
          level: "medium",
          type: "dept",
          dept: dept.department,
          message: `${dept.department} has high patient-to-staff pressure (${waiting} waiting).`,
          icon: "🟡",
          key: `dept_high_${dept.department}`,
        });
      }
    });
    return alerts;
  },

  renderNotifications() {
    const content = document.getElementById("patient-content");

    // 1. User token alerts (≤ 20 min wait)
    const tokenAlerts = this.buildUserTokenAlerts();

    // 2. Dept-level alerts (wait > 60 min or High status)
    const deptAlerts = this._buildDeptAlerts();

    // 3. Backend user notifications
    const backendNotifs = (this.data.notifications || []);
    const backendAlerts = [];
    backendNotifs.forEach(n => {
      const level = String(n.level || "").toLowerCase();
      if (level === "critical" || level === "high") {
        backendAlerts.push({ level: "high", type: "system", dept: n.department || "System", message: n.message || n.title || "", icon: "🔴", key: `sys_${n.id || n.message}` });
      } else if (level === "medium") {
        backendAlerts.push({ level: "medium", type: "system", dept: n.department || "System", message: n.message || n.title || "", icon: "🟡", key: `sys_${n.id || n.message}` });
      }
    });

    // 4. Combine all — token alerts first (most personal), then dept, then backend
    const all = [...tokenAlerts, ...deptAlerts, ...backendAlerts];
    // Deduplicate
    const seen = new Set();
    const alerts = all.filter(a => { if (seen.has(a.key)) return false; seen.add(a.key); return true; });

    const ts = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const hasTokenAlerts = tokenAlerts.length > 0;

    content.innerHTML = `
      <div class="section-header">
        <h2>Notifications / Alerts</h2>
        <p>Your personal queue updates and hospital-wide alerts — auto-refreshed in real time.</p>
        <div style="font-size:12px;color:var(--gray-400);margin-top:4px">Last updated: ${ts}</div>
      </div>

      ${hasTokenAlerts ? `
      <div style="background:linear-gradient(135deg,#fff7ed,#ffedd5);border:1.5px solid #fed7aa;border-radius:14px;padding:16px 20px;margin-bottom:18px;display:flex;align-items:center;gap:14px">
        <div style="font-size:28px;animation:pulse-dot 1.5s infinite">🔔</div>
        <div>
          <div style="font-weight:700;font-size:14px;color:#c2410c">Your Turn is Near!</div>
          <div style="font-size:13px;color:#9a3412;margin-top:2px">You have ${tokenAlerts.length} active token(s) with wait time ≤ 20 minutes. Please be ready at your department.</div>
        </div>
      </div>` : ""}

      <div style="display:flex;flex-direction:column;gap:12px">
        ${alerts.length ? alerts.map((a) => {
          const isToken = a.type === "token";
          const cardBg = isToken
            ? (a.level === "high" ? "background:#fff5f5;border:1.5px solid #fca5a5" : "background:#fff7ed;border:1.5px solid #fed7aa")
            : (a.level === "high" ? "background:#fff5f5;border:1px solid #fecaca" : "background:#fffbeb;border:1px solid #fde68a");
          return `
          <div style="${cardBg};border-radius:13px;padding:16px 18px;display:flex;gap:14px;align-items:flex-start${isToken ? ";box-shadow:0 2px 12px rgba(234,88,12,0.1)" : ""}">
            <div style="font-size:24px;line-height:1.2;flex-shrink:0">${a.icon}</div>
            <div style="flex:1;min-width:0">
              <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                <div style="font-weight:700;font-size:14px;color:${a.level === "high" ? "#b91c1c" : "#92400e"}">${esc(a.dept)} ${isToken ? "Alert" : "Queue Alert"}</div>
                ${isToken ? `<span style="background:${a.level === "high" ? "#fef2f2" : "#fff7ed"};color:${a.level === "high" ? "#dc2626" : "#c2410c"};border:1px solid ${a.level === "high" ? "#fecaca" : "#fed7aa"};border-radius:20px;font-size:10px;font-weight:700;padding:2px 9px">Token ${esc(a.tokenNum)}</span>` : ""}
                ${isToken ? `<span style="background:#f0fdf4;color:#15803d;border:1px solid #bbf7d0;border-radius:20px;font-size:10px;font-weight:700;padding:2px 9px">~${a.waitMin} min wait</span>` : ""}
              </div>
              <div style="font-size:11px;color:var(--gray-400);margin-top:3px">Hospital Queue • ${ts}</div>
              <div style="margin-top:7px;font-size:13px;color:${a.level === "high" ? "#7f1d1d" : "#78350f"};line-height:1.5">${esc(a.message)}</div>
              ${isToken ? `<button class="btn btn-primary btn-sm" style="margin-top:10px;font-size:12px;background:${a.level === "high" ? "#dc2626" : "#ea580c"}" onclick="PatientPortal.navigate('queue')">View Live Queue →</button>` : ""}
            </div>
          </div>`;
        }).join("")
        : `<div class="card" style="text-align:center;padding:48px 20px">
            <div style="font-size:40px;margin-bottom:14px">✅</div>
            <div style="font-weight:700;font-size:16px;color:var(--green-700)">All Clear</div>
            <div style="color:var(--gray-400);margin-top:10px;font-size:13px;max-width:320px;margin-left:auto;margin-right:auto">No alerts right now. When your token wait drops to 20 min or less, you'll see a notification here.</div>
           </div>`}
      </div>`;

    // After rendering, update badge accurately
    this._updateNotifBadge();
  },

  renderDepartments() {
    const content = document.getElementById("patient-content");
    const rows = this.data.departments || [];
    // Merge live queue summary into department data
    const queueMap = {};
    const qs = (this.data.queue && this.data.queue.queue_summary) || (this.data.dashboard && this.data.dashboard.queue_summary) || [];
    qs.forEach((q) => { queueMap[q.department] = q; });

    function alertClass(wait, status) {
      if (wait >= 60 || status === "High") return { cls: "high", label: "High", color: "#ef4444", bg: "#fff5f5", border: "#fecaca" };
      if (wait >= 30 || status === "Medium") return { cls: "medium", label: "Medium", color: "#d97706", bg: "#fffbeb", border: "#fde68a" };
      return { cls: "low", label: "Low", color: "#16a34a", bg: "#f0fdf4", border: "#bbf7d0" };
    }

    content.innerHTML = `
      <div class="section-header">
        <h2>Departments</h2>
        <p>Live queue status for all active departments — updated every 5 seconds.</p>
      </div>
      <div class="patient-grid-3">
        ${rows.map((d) => {
          const q = queueMap[d.name] || {};
          const wait = q.avg_wait_min || 0;
          const waiting = q.waiting || 0;
          const al = alertClass(wait, q.status);
          return `
          <div class="patient-dept-card" style="background:${al.bg};border:1.5px solid ${al.border};cursor:pointer;transition:transform .15s" onmouseenter="this.style.transform='translateY(-3px)'" onmouseleave="this.style.transform=''">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
              <span style="font-weight:800;font-size:15px;color:var(--gray-900)">${esc(d.name)}</span>
              <span style="background:${al.color};color:#fff;font-size:11px;font-weight:700;padding:3px 10px;border-radius:999px">${al.label}</span>
            </div>
            <div style="display:flex;align-items:flex-end;gap:6px;margin-bottom:8px">
              <span style="font-size:32px;font-weight:800;color:${al.color};line-height:1">${wait}</span>
              <span style="font-size:13px;color:var(--gray-500);padding-bottom:4px">min avg wait</span>
            </div>
            <div style="display:flex;gap:18px">
              <div><div style="font-size:18px;font-weight:700;color:var(--gray-800)">${waiting}</div><div style="font-size:11px;color:var(--gray-500)">Waiting</div></div>
              <div><div style="font-size:18px;font-weight:700;color:var(--gray-800)">${d.doctor_count ?? 0}</div><div style="font-size:11px;color:var(--gray-500)">Doctors</div></div>
              <div><div style="font-size:18px;font-weight:700;color:var(--gray-800)">${d.beds ?? 0}</div><div style="font-size:11px;color:var(--gray-500)">Beds</div></div>
            </div>
          </div>`;
        }).join("")}
      </div>`;
  },

  renderDoctors() {
    const content = document.getElementById("patient-content");
    const rows = this.data.doctors || [];

    // Group by department
    const groups = {};
    rows.forEach((d) => {
      const dept = d.department_name || "Other";
      if (!groups[dept]) groups[dept] = [];
      groups[dept].push(d);
    });
    const deptNames = Object.keys(groups).sort();


    function statusStyle(status) {
      if (status === "On Duty") return { bg: "#f0fdf4", border: "#bbf7d0", dot: "#16a34a", label: "On Duty" };
      if (status === "Break")   return { bg: "#fffbeb", border: "#fde68a", dot: "#d97706", label: "Break" };
      return                          { bg: "#fafafa",  border: "#e5e7eb", dot: "#9ca3af", label: status || "Off Duty" };
    }

    const groupsHtml = deptNames.map((dept) => `
      <div style="margin-bottom:28px">
        <div style="font-size:13px;font-weight:700;color:var(--green-700);text-transform:uppercase;letter-spacing:.08em;margin-bottom:12px;padding-bottom:6px;border-bottom:2px solid var(--green-100)">${esc(dept)}</div>
        <div class="patient-grid-3">
          ${groups[dept].map((d) => {
            const st = statusStyle(d.status);
            return `
            <div class="patient-doctor-card" style="background:${st.bg};border:1.5px solid ${st.border}">
              <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
                <div style="width:38px;height:38px;border-radius:50%;background:var(--green-100);display:flex;align-items:center;justify-content:center;font-weight:800;color:var(--green-700);font-size:15px;flex-shrink:0">${esc((d.name || "?").charAt(0).toUpperCase())}</div>
                <div>
                  <div style="font-weight:700;font-size:14px;line-height:1.2">${esc(d.name || "")}</div>
                  <div style="font-size:12px;color:var(--gray-500)">${esc(d.specialization || d.department_name || "")}</div>
                </div>
              </div>
              <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
                <span style="width:8px;height:8px;border-radius:50%;background:${st.dot};flex-shrink:0"></span>
                <span style="font-size:12px;font-weight:600;color:${st.dot}">${st.label}</span>
              </div>
              <div style="font-size:12px;color:var(--gray-500)">🕐 ${esc(d.shift || "—")} Shift</div>
            </div>`;
          }).join("")}
        </div>
      </div>`).join("");

    content.innerHTML = `
      <div class="section-header"><h2>Doctors</h2><p>Find available doctors grouped by department. Choose one when booking or leave open for next available.</p></div>
      <div>${groupsHtml || '<div class="patient-empty">No doctor data available.</div>'}</div>`;
  },

  renderSchedule() {
    const content = document.getElementById("patient-content");

    // Merge live API settings (fetched in refreshCore) with localStorage fallback
    const schedDefaults = {
      morning: { time: "08:00 AM – 04:00 PM", notes: "Morning shift coverage" },
      evening: { time: "04:00 PM – 12:00 AM", notes: "Evening shift coverage" },
      night:   { time: "12:00 AM – 08:00 AM", notes: "Night shift coverage" },
    };
    const contactDefaults = {
      email: "admin@hospital.com",
      phone: "+91 12345 67890",
      address: "Hospital Road, City, State, ZIP",
    };

    // First read from API settings (live), then fallback to localStorage
    const apiSettings = this.data.siteSettings || {};
    let schedData = schedDefaults;
    let contactData = {
      email:   apiSettings.hospital_email   || apiSettings.contact_email   || contactDefaults.email,
      phone:   apiSettings.hospital_phone   || apiSettings.contact_phone   || contactDefaults.phone,
      address: apiSettings.hospital_address || apiSettings.contact_address || contactDefaults.address,
    };
    // Override shift timings from settings if admin set them
    if (apiSettings.shift_morning || apiSettings.morning_shift) {
      schedData.morning = { time: apiSettings.shift_morning || apiSettings.morning_shift || schedDefaults.morning.time, notes: apiSettings.shift_morning_notes || schedDefaults.morning.notes };
    }
    if (apiSettings.shift_evening || apiSettings.evening_shift) {
      schedData.evening = { time: apiSettings.shift_evening || apiSettings.evening_shift || schedDefaults.evening.time, notes: apiSettings.shift_evening_notes || schedDefaults.evening.notes };
    }
    if (apiSettings.shift_night || apiSettings.night_shift) {
      schedData.night = { time: apiSettings.shift_night || apiSettings.night_shift || schedDefaults.night.time, notes: apiSettings.shift_night_notes || schedDefaults.night.notes };
    }
    // Also merge from localStorage (admin may have saved locally)
    try {
      const s = JSON.parse(localStorage.getItem("hq_admin_shift_timings") || "{}");
      if (s.morning) schedData = { ...schedDefaults, ...schedData, ...s };
    } catch (_) {}
    try {
      const c = JSON.parse(localStorage.getItem("hq_admin_contact_info") || "{}");
      if (c.email || c.phone || c.address) contactData = { ...contactData, ...c };
    } catch (_) {}

    const morning = { ...schedDefaults.morning, ...schedData.morning };
    const evening = { ...schedDefaults.evening, ...schedData.evening };
    const night   = { ...schedDefaults.night,   ...schedData.night };
    const email   = contactData.email   || contactDefaults.email;
    const phone   = contactData.phone   || contactDefaults.phone;
    const address = contactData.address || contactDefaults.address;
    const telHref = "tel:" + phone.replace(/\s+/g, "");

    content.innerHTML = `
      <div class="section-header">
        <h2>Schedule &amp; Contact</h2>
        <p>Current hospital shift timings and contact details, managed by admin.</p>
      </div>

      <!-- SHIFT TIMINGS BANNER -->
      <div style="background:linear-gradient(135deg,var(--green-600) 0%,var(--green-800) 100%);border-radius:16px;padding:28px 32px;margin-bottom:22px;color:white;position:relative;overflow:hidden;">
        <div style="position:absolute;top:-40px;right:-40px;width:200px;height:200px;border-radius:50%;background:rgba(255,255,255,0.05);pointer-events:none"></div>
        <div style="position:absolute;bottom:-60px;right:100px;width:140px;height:140px;border-radius:50%;background:rgba(255,255,255,0.04);pointer-events:none"></div>

        <div style="display:flex;align-items:center;gap:12px;margin-bottom:20px">
          <div style="width:38px;height:38px;background:rgba(255,255,255,0.15);border-radius:10px;display:flex;align-items:center;justify-content:center;flex-shrink:0">
            <svg width="20" height="20" fill="none" stroke="white" stroke-width="2" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>
          </div>
          <div>
            <div style="font-family:var(--font-display,sans-serif);font-size:17px;font-weight:700">Shift Timings</div>
            <div style="font-size:11px;opacity:0.72;margin-top:1px;letter-spacing:0.3px">Updated and managed by hospital admin</div>
          </div>
          <div style="margin-left:auto;display:inline-flex;align-items:center;gap:6px;background:rgba(255,255,255,0.14);border-radius:20px;padding:5px 12px;font-size:11px;font-weight:600">
            <span style="width:7px;height:7px;border-radius:50%;background:#6ee7b7;display:inline-block;animation:pulse-dot 2s infinite"></span>
            Live
          </div>
        </div>

        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:14px">
          <div style="background:rgba(255,255,255,0.1);border-radius:13px;padding:18px;border:1px solid rgba(255,255,255,0.12)">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
              <span style="font-size:22px">🌅</span>
              <span style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.2px;opacity:0.8">Morning</span>
            </div>
            <div style="font-size:14px;font-weight:700;line-height:1.4">${esc(morning.time)}</div>
            <div style="font-size:11px;opacity:0.72;margin-top:7px;line-height:1.5">${esc(morning.notes)}</div>
          </div>
          <div style="background:rgba(255,255,255,0.1);border-radius:13px;padding:18px;border:1px solid rgba(255,255,255,0.12)">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
              <span style="font-size:22px">🌆</span>
              <span style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.2px;opacity:0.8">Evening</span>
            </div>
            <div style="font-size:14px;font-weight:700;line-height:1.4">${esc(evening.time)}</div>
            <div style="font-size:11px;opacity:0.72;margin-top:7px;line-height:1.5">${esc(evening.notes)}</div>
          </div>
          <div style="background:rgba(255,255,255,0.1);border-radius:13px;padding:18px;border:1px solid rgba(255,255,255,0.12)">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
              <span style="font-size:22px">🌙</span>
              <span style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.2px;opacity:0.8">Night</span>
            </div>
            <div style="font-size:14px;font-weight:700;line-height:1.4">${esc(night.time)}</div>
            <div style="font-size:11px;opacity:0.72;margin-top:7px;line-height:1.5">${esc(night.notes)}</div>
          </div>
        </div>
      </div>

      <!-- CONTACT US CARD -->
      <div class="card" style="overflow:hidden;padding:0">
        <div style="padding:20px 24px;border-bottom:1px solid var(--gray-100);display:flex;align-items:center;gap:12px">
          <div style="width:36px;height:36px;background:var(--green-100);border-radius:10px;display:flex;align-items:center;justify-content:center;flex-shrink:0">
            <svg width="18" height="18" fill="none" stroke="var(--green-700)" stroke-width="2" viewBox="0 0 24 24"><path d="M22 16.92v3a2 2 0 01-2.18 2 19.79 19.79 0 01-8.63-3.07A19.5 19.5 0 013.07 10.8 19.79 19.79 0 01.1 2.18 2 2 0 012.08 0h3a2 2 0 012 1.72c.127.96.361 1.903.7 2.81a2 2 0 01-.45 2.11L6.91 7.09A16 16 0 0016.9 17.08l.42-.42a2 2 0 012.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0122 16.92z"/></svg>
          </div>
          <div>
            <div style="font-size:15px;font-weight:700;color:var(--gray-800)">Contact Us</div>
            <div style="font-size:12px;color:var(--gray-400);margin-top:1px">Reach out to the hospital directly</div>
          </div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr">
          <!-- Email -->
          <div style="padding:22px 24px;border-right:1px solid var(--gray-100)">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
              <div style="width:32px;height:32px;background:var(--green-50);border-radius:8px;display:flex;align-items:center;justify-content:center;flex-shrink:0">
                <svg width="16" height="16" fill="none" stroke="var(--green-600)" stroke-width="2" viewBox="0 0 24 24"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
              </div>
              <span style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--gray-400)">Email</span>
            </div>
            <div style="font-size:14px;font-weight:600;color:var(--green-700);word-break:break-all;margin-bottom:10px">${esc(email)}</div>
            <a href="mailto:${esc(email)}" style="font-size:12px;color:var(--green-600);font-weight:600;text-decoration:none;display:inline-flex;align-items:center;gap:5px;padding:5px 10px;background:var(--green-50);border-radius:7px;border:1px solid var(--green-200)">
              <svg width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
              Send Email
            </a>
          </div>
          <!-- Phone -->
          <div style="padding:22px 24px;border-right:1px solid var(--gray-100)">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
              <div style="width:32px;height:32px;background:var(--green-50);border-radius:8px;display:flex;align-items:center;justify-content:center;flex-shrink:0">
                <svg width="16" height="16" fill="none" stroke="var(--green-600)" stroke-width="2" viewBox="0 0 24 24"><path d="M22 16.92v3a2 2 0 01-2.18 2 19.79 19.79 0 01-8.63-3.07A19.5 19.5 0 013.07 10.8 19.79 19.79 0 01.1 2.18 2 2 0 012.08 0h3a2 2 0 012 1.72c.127.96.361 1.903.7 2.81a2 2 0 01-.45 2.11L6.91 7.09A16 16 0 0016.9 17.08l.42-.42a2 2 0 012.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0122 16.92z"/></svg>
              </div>
              <span style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--gray-400)">Phone</span>
            </div>
            <div style="font-size:14px;font-weight:600;color:var(--gray-800);margin-bottom:10px">${esc(phone)}</div>
            <a href="${esc(telHref)}" style="font-size:12px;color:var(--green-600);font-weight:600;text-decoration:none;display:inline-flex;align-items:center;gap:5px;padding:5px 10px;background:var(--green-50);border-radius:7px;border:1px solid var(--green-200)">
              <svg width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M22 16.92v3a2 2 0 01-2.18 2 19.79 19.79 0 01-8.63-3.07A19.5 19.5 0 013.07 10.8 19.79 19.79 0 01.1 2.18 2 2 0 012.08 0h3a2 2 0 012 1.72c.127.96.361 1.903.7 2.81a2 2 0 01-.45 2.11L6.91 7.09A16 16 0 0016.9 17.08l.42-.42a2 2 0 012.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0122 16.92z"/></svg>
              Call Now
            </a>
          </div>
          <!-- Address -->
          <div style="padding:22px 24px">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
              <div style="width:32px;height:32px;background:var(--green-50);border-radius:8px;display:flex;align-items:center;justify-content:center;flex-shrink:0">
                <svg width="16" height="16" fill="none" stroke="var(--green-600)" stroke-width="2" viewBox="0 0 24 24"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0118 0z"/><circle cx="12" cy="10" r="3"/></svg>
              </div>
              <span style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--gray-400)">Address</span>
            </div>
            <div style="font-size:13px;color:var(--gray-700);line-height:1.7;white-space:pre-wrap">${esc(address)}</div>
          </div>
        </div>
      </div>

      <!-- INFO NOTE -->
      <div style="margin-top:18px;padding:13px 18px;background:var(--green-50);border:1px solid var(--green-100);border-radius:10px;display:flex;align-items:center;gap:10px">
        <svg width="15" height="15" fill="none" stroke="var(--green-600)" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>
        <span style="font-size:12px;color:var(--green-700)">Shift timings and contact details are maintained by the hospital admin and reflect the latest saved information.</span>
      </div>`;
  },

  renderSettings() {
    const content = document.getElementById("patient-content");
    const s = this.settings;
    content.innerHTML = `
      <div class="section-header"><h2>Settings</h2><p>Control how your user portal behaves on this device.</p></div>
      <div class="card" style="max-width:760px">
        <div class="patient-settings-line">
          <div><div style="font-weight:700">Toast notifications</div><div class="patient-meta">Show small success and queue alerts in the interface.</div></div>
          <div class="patient-toggle ${s.toasts ? "on" : ""}" onclick="PatientPortal.toggleSetting('toasts', this)"></div>
        </div>
        <div class="patient-settings-line">
          <div><div style="font-weight:700">Live queue auto-refresh</div><div class="patient-meta">Keep queue status refreshed automatically.</div></div>
          <div class="patient-toggle ${s.liveRefresh ? "on" : ""}" onclick="PatientPortal.toggleSetting('liveRefresh', this)"></div>
        </div>
        <div class="patient-settings-line">
          <div><div style="font-weight:700">Compact queue cards</div><div class="patient-meta">Reduce spacing in queue-heavy screens on mobile.</div></div>
          <div class="patient-toggle ${s.compact ? "on" : ""}" onclick="PatientPortal.toggleSetting('compact', this)"></div>
        </div>
        <div style="display:flex;gap:10px;margin-top:16px">
          <button class="btn btn-primary" onclick="PatientPortal.saveSettings()">Save Settings</button>
          <button class="btn btn-danger" onclick="doLogout()">Logout</button>
        </div>
      </div>`;
  },

  selectedDepartmentId() {
    const current = document.getElementById("book-department");
    if (current && current.value) return current.value;
    return this.data.departments?.[0]?.id || "";
  },

  filteredDoctors() {
    const deptId = String(this.selectedDepartmentId());
    return (this.data.doctors || []).filter((d) => !deptId || String(d.department_id) === deptId);
  },

  bookingFormValue(id, fallback = "") {
    const el = document.getElementById(id);
    return el ? el.value : fallback;
  },

  selectedBookingTime() {
    const current = document.getElementById("book-time");
    if (current && current.value) return current.value;
    const now = new Date();
    const minutes = now.getMinutes() < 30 ? "00" : "30";
    return `${String(now.getHours()).padStart(2, "0")}:${minutes}`;
  },

  bookingTimeSlots(doctors, doctorId, selectedValue) {
    const selectedDoctor = (doctors || []).find((doc) => String(doc.id) === String(doctorId));
    const shiftRanges = { Morning: [6, 12], Evening: [12, 18], Night: [18, 24] };
    const [startHour, endHour] = selectedDoctor && shiftRanges[selectedDoctor.shift] ? shiftRanges[selectedDoctor.shift] : [0, 24];
    const options = [];
    for (let hour = startHour; hour < endHour; hour += 1) {
      for (const minute of [0, 30]) {
        const value = `${String(hour).padStart(2, "0")}:${minute === 0 ? "00" : "30"}`;
        const labelHour = ((hour + 11) % 12) + 1;
        const label = `${labelHour}:${minute === 0 ? "00" : "30"} ${hour < 12 ? "AM" : "PM"}`;
        options.push(`<option value="${value}" ${value === selectedValue ? "selected" : ""}>${label}</option>`);
      }
    }
    const hasSelected = options.some((option) => option.includes(`value="${selectedValue}"`));
    return {
      options: (selectedValue && !hasSelected ? `<option value="${selectedValue}" selected>${selectedValue}</option>` : "") + options.join(""),
      hint: selectedDoctor
        ? `${selectedDoctor.shift || "Selected"} shift doctor chosen — visit times follow that shift`
        : "Any available doctor selected — choose any visit time",
    };
  },

  bookingResultHtml(token, appointment) {
    return `
      <div style="background:linear-gradient(135deg,var(--green-500),var(--green-700));border-radius:12px;padding:24px;text-align:center;color:white">
        <div style="font-size:11px;opacity:0.75;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Token Generated</div>
        <div style="font-family:var(--font-display);font-size:56px;font-weight:800;line-height:1">${esc(token?.token_number || "—")}</div>
        <div style="font-size:14px;opacity:0.85;margin-top:4px">${esc(appointment?.department_name || token?.department_name || "—")}</div>
        <div style="font-size:12px;opacity:0.7;margin-top:8px">Est. wait: ${token?.est_wait_min || 0} min</div>
      </div>`;
  },

  queueFilterValue(id, fallback = "") {
    const el = document.getElementById(id);
    return el ? el.value : fallback;
  },

  resetQueueFilters() {
    ["uq-dept-filter", "uq-doctor-filter", "uq-urgency-filter", "uq-search"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.value = "";
    });
    this.renderQueue();
  },

  // ── Silently upgrade demo_user_token to a real session ──────────────────
  // Also silently re-login if current token is stale (any authToken)
  async _ensureRealToken() {
    const isDemoToken = (typeof authToken !== "undefined") && authToken === "demo_user_token";
    const hasNoToken = (typeof authToken === "undefined") || !authToken;
    // Try silent re-login whenever we have saved credentials (demo OR missing token)
    if (!hasNoToken && !isDemoToken) return; // already have a real token — nothing to do
    try {
      const savedUser = JSON.parse(localStorage.getItem("hq_user") || "null");
      const savedPass = localStorage.getItem("hq_saved_pass") || "";
      if (!savedUser || !savedPass) return;
      const base = (typeof API_BASE !== "undefined" ? API_BASE : "");
      const res = await fetch(base + "/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: savedUser.username || savedUser.email, password: savedPass }),
      });
      if (!res.ok) return;
      const json = await res.json();
      if (json.success && json.data && json.data.token) {
        authToken = json.data.token;
        localStorage.setItem("hq_auth_token", authToken);
        // Merge new user data but keep display_name if backend doesn't return it
        const mergedUser = Object.assign({}, savedUser, json.data.user);
        localStorage.setItem("hq_user", JSON.stringify(mergedUser));
        console.log("[PatientPortal] Session upgraded/refreshed successfully");
      }
    } catch (e) {
      console.warn("[PatientPortal] _ensureRealToken failed:", e.message);
    }
  },

  // ── Push any pending offline bookings to the backend ─────────────────────
  async _syncOfflineBookings() {
    if (authToken === "demo_user_token") return; // still offline
    try {
      const pending = JSON.parse(localStorage.getItem("hq_pending_bookings") || "[]");
      if (!pending.length) return;
      const remaining = [];
      for (const item of pending) {
        try {
          await apiPost("/api/user/appointments", item.payload);
          console.log("[PatientPortal] Synced offline booking:", item.payload.name, item.payload.department_id);
        } catch (e) {
          remaining.push(item); // keep for next attempt
        }
      }
      localStorage.setItem("hq_pending_bookings", JSON.stringify(remaining));
      if (remaining.length < pending.length) {
        // Some synced — refresh admin queue panels too
        if (typeof fetchQueueSummary === "function") fetchQueueSummary().catch(() => {});
        if (typeof loadDashboardQueueTable === "function") loadDashboardQueueTable().catch(() => {});
      }
    } catch (_) {}
  },

  async submitBooking() {
    // ── Validate required fields ────────────────────────────────────────────
    const nameVal  = (document.getElementById("book-name")?.value || "").trim();
    const phoneVal = (document.getElementById("book-phone")?.value || "").trim();
    const deptVal  = (document.getElementById("book-department")?.value || "").trim();
    if (!nameVal)  { safeToast("Patient name is required", "error"); document.getElementById("book-name")?.focus(); return; }
    if (!phoneVal) { safeToast("Phone number is required", "error"); document.getElementById("book-phone")?.focus(); return; }
    if (!deptVal)  { safeToast("Please select a department", "error"); return; }

    const btn = document.querySelector("#book-form ~ div .btn-primary") ||
                document.querySelector("button[onclick*='submitBooking']");
    const origText = btn ? btn.innerHTML : "";
    if (btn) { btn.innerHTML = "⏳ Generating Token…"; btn.disabled = true; }

    // ── Dept / doctor labels (needed for offline path) ──────────────────────
    const deptSelect   = document.getElementById("book-department");
    const deptName     = deptSelect?.options[deptSelect.selectedIndex]?.text || "Department";
    const doctorSelect = document.getElementById("book-doctor");
    const doctorName   = doctorSelect?.options[doctorSelect.selectedIndex]?.text || "Any available doctor";

    const payload = {
      name:          nameVal,
      age:           (document.getElementById("book-age")?.value || "").trim(),
      gender:        document.getElementById("book-gender")?.value || "Other",
      department_id: deptVal,
      doctor_id:     document.getElementById("book-doctor")?.value || null,
      visit_date:    document.getElementById("book-date")?.value || todayISO(),
      visit_time:    document.getElementById("book-time")?.value || "",
      urgency:       document.getElementById("book-urgency")?.value || "Medium",
      phone:         phoneVal,
      symptoms:      (document.getElementById("book-symptoms")?.value || "").trim(),
    };

    try {
      // ── Step 1: Try to upgrade demo token to a real session ────────────────
      await this._ensureRealToken();

      let result = null;
      let usedOffline = false;

      // ── Step 2: Try live API (works for real-token users AND newly-upgraded) ─
      if (typeof authToken !== "undefined" && authToken && authToken !== "demo_user_token") {
        try {
          result = await apiPost("/api/user/appointments", payload);
        } catch (apiErr) {
          console.warn("[Booking] API failed:", apiErr.message);
          result = null;
        }
      }

      // ── Step 3: After a live booking, refresh both portals ─────────────────
      if (result) {
        // Immediately bump the local queueCache so user Live Queue Status shows
        // the new waiting count before the next server poll arrives
        if (typeof queueCache !== "undefined" && queueCache[deptName]) {
          queueCache[deptName].waiting = (queueCache[deptName].waiting || 0) + 1;
        }
        // Force admin queue panels to refresh so they see the new appointment
        setTimeout(async () => {
          try {
            if (typeof fetchQueueSummary === "function") await fetchQueueSummary();
            if (typeof loadDashboardQueueTable === "function") await loadDashboardQueueTable();
            if (typeof renderTokenQueue === "function") await renderTokenQueue();
          } catch (_) {}
        }, 400);
      }

      // ── Step 4: Offline fallback — local token + queue pending sync ─────────
      if (!result) {
        usedOffline = true;

        const prefixMap = {
          cardiology: "CA", orthopedics: "OR", "general medicine": "GM",
          pediatrics: "PE", dermatology: "DE", neurology: "NE",
          emergency: "EM", radiology: "RA", pathology: "PA"
        };
        const key = deptName.toLowerCase();
        const prefix = Object.keys(prefixMap).find(k => key.includes(k))
          ? prefixMap[Object.keys(prefixMap).find(k => key.includes(k))]
          : deptName.substring(0, 2).toUpperCase();

        let existingNums = [];
        try {
          const hist = JSON.parse(localStorage.getItem("user_appt_history") || "[]");
          existingNums = hist
            .filter(a => String(a.dept || "").toLowerCase() === deptName.toLowerCase() && String(a.token || "").startsWith(prefix))
            .map(a => parseInt(String(a.token).replace(prefix, ""), 10))
            .filter(n => !isNaN(n));
        } catch (_) {}
        const nextNum = existingNums.length ? Math.max(...existingNums) + 1 : Math.floor(Math.random() * 80) + 5;
        const tokenNum = prefix + String(nextNum).padStart(2, "0");

        const qCache = (typeof queueCache !== "undefined" && queueCache[deptName]) || {};
        const waitMin = qCache.avg_wait_min || 20;
        const position = (qCache.waiting || 0) + 1;
        const apptId   = "local_" + Date.now();
        const visitDate = payload.visit_date || todayISO();

        // Save appointment to local history
        const histEntry = {
          id: apptId, token: tokenNum, token_number: tokenNum,
          dept: deptName, department_name: deptName,
          doctor: doctorName !== "Any available doctor" ? doctorName : "Any",
          doctor_name: doctorName !== "Any available doctor" ? doctorName : null,
          date: visitDate, visit_date: visitDate,
          wait: waitMin + " min", est_wait_min: waitMin, token_est_wait_min: waitMin,
          status: "Scheduled", queue_status: "Waiting", _source: "offline",
        };
        try {
          const hist = JSON.parse(localStorage.getItem("user_appt_history") || "[]");
          hist.unshift(histEntry);
          localStorage.setItem("user_appt_history", JSON.stringify(hist));
        } catch (_) {}

        // Save as active token (include all fields needed by _mergeLocalAppointments)
        const activeToken = {
          token: tokenNum, token_number: tokenNum,
          dept: deptName, department_name: deptName,
          doctor: histEntry.doctor, doctor_name: histEntry.doctor_name,
          date: visitDate, visit_date: visitDate,
          wait: waitMin + " min", est_wait_min: waitMin, token_est_wait_min: waitMin,
          status: "Scheduled", queue_status: "Waiting",
          position, created: new Date().toISOString(), appointment_id: apptId,
        };
        try {
          const tokens = JSON.parse(localStorage.getItem("user_active_tokens") || "[]");
          tokens.push(activeToken);
          localStorage.setItem("user_active_tokens", JSON.stringify(tokens));
          localStorage.setItem("user_active_token", JSON.stringify(activeToken));
        } catch (_) {}

        // Queue this booking for sync once the user gets a real token
        try {
          const pending = JSON.parse(localStorage.getItem("hq_pending_bookings") || "[]");
          pending.push({ payload, localId: apptId, ts: Date.now() });
          localStorage.setItem("hq_pending_bookings", JSON.stringify(pending));
        } catch (_) {}

        result = {
          token: { token_number: tokenNum, est_wait_min: waitMin, position },
          appointment: { id: apptId, department_name: deptName, visit_date: visitDate, status: "Scheduled" },
        };

        this.data.appointments = this.data.appointments || [];
        this.data.appointments.unshift({
          id: apptId, token_number: tokenNum, department_name: deptName,
          doctor_name: histEntry.doctor_name, visit_date: visitDate,
          status: "Scheduled", queue_status: "Waiting",
          token_est_wait_min: waitMin, est_wait_min: waitMin,
        });
      }

      // ── Step 5: Show success UI ─────────────────────────────────────────────
      if (!usedOffline) {
        await this.refreshCore();
      }
      const box = document.getElementById("book-result");
      if (box) {
        box.style.display = "block";
        box.innerHTML = `
          ${this.bookingResultHtml(result.token, result.appointment)}
          <div style="margin-top:14px;display:flex;gap:10px">
            <button class="btn btn-outline" onclick="PatientPortal.navigate('queue')" style="flex:1">View Live Queue</button>
            <button class="btn btn-ghost" onclick="PatientPortal.renderBooking()" style="flex:1">New Booking</button>
          </div>`;
      }
      safeToast("✅ Token " + (result.token?.token_number || "") + " generated for " + deptName);
      this.paintUserHeader();

    } catch (e) {
      safeToast("Booking failed: " + e.message, "error");
    } finally {
      if (btn) { btn.innerHTML = origText || "Register &amp; Generate Token"; btn.disabled = false; }
    }
  },

  toggleSetting(key, el) {
    this.settings[key] = !this.settings[key];
    if (el) el.classList.toggle("on", !!this.settings[key]);
  },

  saveSettings() {
    localStorage.setItem("hq_patient_settings", JSON.stringify(this.settings));
    safeToast("Settings saved");
  },

  connectStream() {
    if (!this.settings.liveRefresh || this.stream || !this.isPatientUser()) return;
    try {
      this.stream = new EventSource("/api/stream");
      this.stream.addEventListener("queue_update", () => this.scheduleStreamRefresh());
      this.stream.addEventListener("snapshot", () => this.scheduleStreamRefresh());
      this.stream.addEventListener("alert", () => this.scheduleStreamRefresh());
      this.stream.onerror = () => {
        if (this.stream) this.stream.close();
        this.stream = null;
        clearTimeout(this.streamRetry);
        this.streamRetry = setTimeout(() => this.connectStream(), 2500);
      };
    } catch (e) {
      console.warn("Patient stream unavailable", e);
    }
  },

  disconnectStream() {
    if (this.stream) this.stream.close();
    this.stream = null;
    clearTimeout(this.streamRetry);
  },

  scheduleStreamRefresh() {
    if (!this.settings.liveRefresh) return;
    clearTimeout(this.streamRefresh);
    this.streamRefresh = setTimeout(async () => {
      await this.refreshCore();
      await this.navigate(this.activePage);
    }, 500);
  },
};

function loadPatientSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem("hq_patient_settings") || "{}");
    return {
      toasts: saved.toasts !== false,
      liveRefresh: saved.liveRefresh !== false,
      compact: saved.compact === true,
    };
  } catch (_) {
    return { toasts: true, liveRefresh: true, compact: false };
  }
}

function safeToast(message, type = "info") {
  if (PatientPortal.settings.toasts === false) return;
  if (typeof showToast === "function") showToast(message, type);
}

// ------------------------------------------------------------
// Login / registration enhancements
// ------------------------------------------------------------
function installUserLoginEnhancements() {
  const tabs = document.querySelectorAll(".role-tab");
  if (tabs[1]) tabs[1].textContent = "User";

  const hint = document.getElementById("login-hint");
  if (hint && window.currentRole === "user") {
    hint.textContent = "User: sign in with your registered account or create one below.";
  }

  if (!document.getElementById("open-register-btn")) {
    const btn = document.querySelector(".login-btn");
    if (btn) {
      btn.insertAdjacentHTML(
        "afterend",
        `
        <button id="open-register-btn" class="btn btn-ghost" style="width:100%;justify-content:center;margin-top:12px;display:none" onclick="openRegisterModal()">
          Create New User Account
        </button>`
      );
    }
  }

  if (typeof window.selectRole === "function") {
    window.selectRole = function(role, btn) {
      window.currentRole = role;

      document.querySelectorAll(".role-tab").forEach((t) => t.classList.remove("active"));
      if (btn) btn.classList.add("active");

      const roleHint = document.getElementById("login-hint");
      if (roleHint) {
        roleHint.textContent = role === "admin"
          ? "Admin: admin / Password: 1234"
          : "User: sign in with your registered account or create one below.";
      }

      const regBtn = document.getElementById("open-register-btn");
      if (regBtn) regBtn.style.display = role === "admin" ? "none" : "inline-flex";
    };
  }

  window.doLogin = async function() {
    const user = document.getElementById("login-user").value.trim().toLowerCase();
    const pass = document.getElementById("login-pass").value.trim();

    if (!user || !pass) {
      safeToast("Please enter username and password", "error");
      return;
    }

    const btn = document.querySelector(".login-btn");
    const original = btn.textContent;
    btn.textContent = "Signing in...";
    btn.disabled = true;

    let session;
    try {
      session = await apiPost("/api/auth/login", { username: user, password: pass });
    } catch (e) {
      safeToast(e.message || "Login failed", "error");
      btn.textContent = original;
      btn.disabled = false;
      return;
    }

    btn.textContent = original;
    btn.disabled = false;

    if (window.currentRole === "admin" && session.user.role !== "admin") {
      safeToast("This account does not have Admin access", "error");
      return;
    }

    if (window.currentRole === "user" && session.user.role === "admin") {
      safeToast("Please use the Admin tab to sign in as an administrator", "error");
      return;
    }

    authToken = session.token;
    localStorage.setItem("hq_auth_token", authToken);
    localStorage.setItem("hq_user", JSON.stringify(session.user));
    localStorage.setItem("hq_saved_pass", pass);

    if (window.currentRole === "admin") {
      showPage("admin");
      if (typeof adminNav === "function") {
        adminNav("dashboard", document.querySelector("#admin-page .nav-item"));
      }
      await Promise.allSettled([
        loadDashboardStats(),
        loadDashboardQueueTable(),
        loadTodayAppointmentsTable()
      ]);
    } else {
      await PatientPortal.boot(true);
    }
  };
}

function openRegisterModal() {
  const existing = document.getElementById("register-modal");
  if (existing) existing.remove();

  const modal = document.createElement("div");
  modal.id = "register-modal";
  modal.className = "modal-overlay";
  modal.style.display = "flex";

  modal.innerHTML = `
    <div class="modal-box" style="width:min(760px,92vw)">
      <div class="modal-title">
        <span>Create User Account</span>
        <button class="modal-close" onclick="document.getElementById('register-modal').remove()">×</button>
      </div>

      <div class="patient-form-grid" style="padding:4px 2px">
        <div>
          <label class="form-label-sm">Full Name</label>
          <input class="form-input-sm" id="reg-name" autocomplete="name">
        </div>

        <div>
          <label class="form-label-sm">Username</label>
          <input class="form-input-sm" id="reg-username" autocomplete="username">
        </div>

        <div>
          <label class="form-label-sm">Password</label>
          <input class="form-input-sm" id="reg-password" type="password" autocomplete="new-password">
        </div>

        <div>
          <label class="form-label-sm">Phone</label>
          <input class="form-input-sm" id="reg-phone" autocomplete="tel">
        </div>

        <div>
          <label class="form-label-sm">Email</label>
          <input class="form-input-sm" id="reg-email" type="email" autocomplete="email">
        </div>

        <div>
          <label class="form-label-sm">Age</label>
          <input class="form-input-sm" id="reg-age" type="number">
        </div>

        <div>
          <label class="form-label-sm">Gender</label>
          <select class="form-select" id="reg-gender">
            <option>Male</option>
            <option>Female</option>
            <option>Other</option>
          </select>
        </div>

        <div class="full">
          <label class="form-label-sm">Address</label>
          <textarea class="form-input-sm" id="reg-address" rows="3"></textarea>
        </div>
      </div>

      <div style="display:flex;gap:10px;margin-top:16px">
        <button class="btn btn-primary" onclick="submitRegistration()">Create Account</button>
        <button class="btn btn-ghost" onclick="document.getElementById('register-modal').remove()">Cancel</button>
      </div>
    </div>`;

  document.body.appendChild(modal);
}

async function submitRegistration() {
  try {
    const payload = {
      name: document.getElementById("reg-name").value.trim(),
      username: document.getElementById("reg-username").value.trim().toLowerCase(),
      password: document.getElementById("reg-password").value.trim(),
      phone: document.getElementById("reg-phone").value.trim(),
      email: document.getElementById("reg-email").value.trim(),
      age: document.getElementById("reg-age").value.trim(),
      gender: document.getElementById("reg-gender").value,
      address: document.getElementById("reg-address").value.trim(),
      role: "user"
    };

    const result = await apiPost("/api/auth/register", payload);

    document.getElementById("register-modal")?.remove();

    if (result?.verification_email_sent) {
      safeToast("Verification email sent. Please check your inbox and click the link before logging in.");
    } else if (result?.verification_link) {
      safeToast("SMTP is not configured. Open the verification link shown by the backend response.");
      console.log("Verification link:", result.verification_link);
    } else {
      safeToast("Account created. Please check your email to verify your account.");
    }

    showAuthPage("user-login");
    const loginUser = document.getElementById("login-user");
    if (loginUser) loginUser.value = payload.username;
  } catch (e) {
    safeToast("Registration failed: " + e.message, "error");
  }
}

// ------------------------------------------------------------
// Boot
// ------------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
  setTimeout(() => {
    _installAdminNavHook();
    _patchSubmitNewAppointment();
    _patchStaffCreateAppointment();
    installUserLoginEnhancements();
  }, 150);

  setTimeout(refreshActivePage, 600);
  setInterval(refreshActivePage, 5000);

  // Every 15 sec: refresh user appointments + check token alerts
  setInterval(async () => {
    const userPage = document.getElementById("user-page");
    if (!userPage || userPage.style.display === "none") return;
    if (PatientPortal && PatientPortal.isPatientUser()) {
      try {
        const appts = await apiGet("/api/user/appointments").catch(() => null);
        if (appts && Array.isArray(appts)) {
          // Only replace if API returned real data; never blank out what's already shown
          if (appts.length > 0 || !PatientPortal.data.appointments?.length) {
            PatientPortal.data.appointments = appts;
          }
        }
        // Always re-merge localStorage appointments so offline/pending bookings stay visible
        PatientPortal._mergeLocalAppointments();
        // Also refresh queue summary for dept alerts
        const qs = await apiGet("/api/dashboard/queue-summary").catch(() => null);
        if (qs && Array.isArray(qs)) {
          if (!PatientPortal.data.dashboard) PatientPortal.data.dashboard = {};
          PatientPortal.data.dashboard.queue_summary = qs;
        }
        PatientPortal.checkAndPushUserTokenAlerts();
        // Re-render live if on appointments or notifications page
        if (PatientPortal.activePage === "appointments") {
          PatientPortal.renderAppointments();
        } else if (PatientPortal.activePage === "notifications") {
          PatientPortal.renderNotifications();
        }
      } catch (e) { /* ignore */ }
    }
  }, 15000);

  setTimeout(async () => {
    const savedUser = JSON.parse(localStorage.getItem("hq_user") || "null");
    if (savedUser && savedUser.role !== "admin") {
      const userPageVisible = document.getElementById("user-page")?.style.display !== "none";
      await PatientPortal.boot(userPageVisible);
    }
  }, 700);
});

window.LiveData = {
  loadDashboardStats,
  loadDashboardQueueTable,
  loadDoctorsTable,
  loadTodayAppointmentsTable,
  refreshActivePage,
  refreshAllPanels,
};

window.PatientPortal = PatientPortal;
window.openRegisterModal = openRegisterModal;
window.submitRegistration = submitRegistration;
window.fetchDashboardStats = loadDashboardStats;
window.updateDashboardStats = loadDashboardStats;
window.renderDoctors = loadDoctorsTable;