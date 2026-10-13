// SNAP setup wizard (Design 0001, section 5.3 and the GUI draft).
//
// Runs in the Proxmox installer's WebKit window (snap-wizard.pl). It starts
// with SNAP's settings from the stick filled in, so pressing Next all the way
// gives the built-in lab. Talks to snap-wizard.pl through "snap:" links:
//   snap:ready    -> the window answers with SNAP.init(data)
//   snap:install  -> install with these settings; answers with SNAP.progress(),
//                    SNAP.installFailed() or SNAP.installDone()
//   snap:recheck, snap:poweroff
// Tests replace the link with window.SNAP_BRIDGE(action, payload).
"use strict";

const SNAP = (() => {
  const STEPS = ["Host", "Network", "Machines", "Firewall", "Review"];
  const ORDER = ["host", "network", "machines", "firewall", "review"];
  const ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789";
  const KEYBOARDS = [
    ["en-us", "English (US)"], ["en-gb", "English (UK)"], ["de", "German"], ["de-ch", "Swiss German"],
    ["fr", "French"], ["fr-be", "French (Belgium)"], ["fr-ca", "French (Canada)"], ["fr-ch", "Swiss French"],
    ["es", "Spanish"], ["it", "Italian"], ["pt", "Portuguese"], ["pt-br", "Portuguese (Brazil)"],
    ["nl", "Dutch"], ["da", "Danish"], ["fi", "Finnish"], ["no", "Norwegian"], ["se", "Swedish"],
    ["is", "Icelandic"], ["pl", "Polish"], ["hu", "Hungarian"], ["si", "Slovenian"], ["lt", "Lithuanian"],
    ["mk", "Macedonian"], ["tr", "Turkish"], ["jp", "Japanese"],
  ];
  const POLICIES = [
    ["isolated", "Isolated", "Only its own network."],
    ["internet-only", "Internet only", "The internet, not your home network or other lab networks."],
    ["open", "Open", "The internet and the other lab networks."],
  ];
  const NAME_RE = /^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$/;
  const DOMAIN_RE = /^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;
  const USER_RE = /^[a-z_][a-z0-9_-]{0,31}$/;
  const KEY_RE = /^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/=]+( .*)?$/;
  const ADMIN_VLAN = 4094;

  let data = null; // what the window sent: settings from the stick, disks, checks, ...
  let s = null; // the settings being edited
  let screen = "loading";
  let errors = {};
  let editNet = null; // {index, net} while a network is being edited
  let newRule = null; // the rule being added
  let installLog = [];

  // --- helpers ---------------------------------------------------------------

  const $ = (sel) => document.querySelector(sel);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const clone = (v) => JSON.parse(JSON.stringify(v));

  function send(action, payload) {
    if (window.SNAP_BRIDGE) return window.SNAP_BRIDGE(action, payload || {});
    location.href = "snap:" + action + "?" + encodeURIComponent(JSON.stringify(payload || {}));
  }

  function makePassword() {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    let out = "";
    bytes.forEach((b, i) => { out += (i && i % 4 === 0 ? "-" : "") + ALPHABET[b % ALPHABET.length]; });
    return out;
  }

  function ip(text) {
    const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(String(text).trim());
    if (!m) return null;
    const parts = m.slice(1).map(Number);
    if (parts.some((p) => p > 255)) return null;
    return ((parts[0] << 24) >>> 0) + (parts[1] << 16) + (parts[2] << 8) + parts[3];
  }
  const ipText = (n) => [n >>> 24, (n >>> 16) & 255, (n >>> 8) & 255, n & 255].join(".");

  function cidr(text) {
    const m = /^([\d.]+)\/(\d{1,2})$/.exec(String(text).trim());
    if (!m) return null;
    const base = ip(m[1]), prefix = Number(m[2]);
    if (base === null || prefix > 32) return null;
    const size = 2 ** (32 - prefix);
    if (base % size !== 0) return { bad: "has host bits set; use " + ipText(base - (base % size)) + "/" + prefix };
    return { base, prefix, size, last: base + size - 1 };
  }
  const usable = (net, addr) => addr !== null && addr > net.base && addr < net.last;
  const overlaps = (a, b) => a.base <= b.last && b.base <= a.last;

  // --- start -----------------------------------------------------------------

  function init(d) {
    data = d;
    const c = d.config;
    s = {
      host: {
        hostname: c.host.hostname, domain: c.host.domain, timezone: c.host.timezone,
        keyboard: c.host.keyboard, country: c.host.country,
        mode: "dhcp", address: "", gateway: "", dns: "", iface: (d.interfaces[0] || {}).name || "",
      },
      password: makePassword(),
      networks: c.networks.map((n) => Object.assign({ vlan: "", policy: "isolated", rules: [] }, clone(n),
        { dhcp: n.dhcp && n.dhcp.enabled ? clone(n.dhcp) : { enabled: false, start: "", end: "" } })),
      machines: Object.assign(clone(c.machines), {
        memory_gb: Math.round((c.machines.memory_mb || 2048) / 1024),
        ssh_key: (c.machines.ssh_keys || [])[0] || "",
      }),
      disk: (d.disks[0] || {}).name || "",
      understood: false,
    };
    const blocking = (d.checks || []).some((x) => x.level === "fix");
    go(blocking ? "check" : "host");
    if (d.auto) autoRun();
  }

  // Unattended test run (kernel option snap.wizard=auto): defaults and the first disk.
  function autoRun() {
    let i = 0;
    const tick = () => {
      if (screen === "check") return;
      if (screen === "review") { s.understood = true; render(); setTimeout(() => start(), 1500); return; }
      next();
      if (++i < 10) setTimeout(tick, 1500);
    };
    setTimeout(tick, 1500);
  }

  function go(name) { screen = name; errors = {}; editNet = null; newRule = null; render(); }

  function next() {
    readForm();
    errors = validate(screen);
    if (Object.keys(errors).length) { render(); return; }
    const i = ORDER.indexOf(screen);
    if (i >= 0 && i < ORDER.length - 1) go(ORDER[i + 1]);
  }

  function back() { readForm(); const i = ORDER.indexOf(screen); if (i > 0) go(ORDER[i - 1]); }

  function start() {
    readForm();
    for (const name of ORDER) {
      const e = validate(name);
      if (Object.keys(e).length) { screen = name; errors = e; render(); return; }
    }
    installLog = [];
    screen = "installing";
    render();
    send("install", payload());
  }

  // --- reading the form -------------------------------------------------------

  function val(id) { const el = document.getElementById(id); return el ? el.value.trim() : undefined; }

  function readForm() {
    const h = s.host;
    if (screen === "host") {
      for (const k of ["hostname", "domain", "timezone", "keyboard", "mode", "address", "gateway", "dns", "iface"]) {
        const v = val("h-" + k); if (v !== undefined) h[k] = v;
      }
      h.hostname = h.hostname.toLowerCase(); h.domain = h.domain.toLowerCase();
      const pw = document.getElementById("h-password"); if (pw) s.password = pw.value;
    } else if (screen === "network" && editNet) {
      const n = editNet.net;
      n.name = (val("n-name") || "").toLowerCase(); n.cidr = val("n-cidr"); n.router = val("n-router");
      n.vlan = val("n-vlan");
      n.dhcp = { enabled: document.getElementById("n-dhcp").checked, start: val("n-start"), end: val("n-end") };
    } else if (screen === "machines") {
      const m = s.machines;
      m.count = val("m-count"); m.name_prefix = (val("m-prefix") || "").toLowerCase(); m.cpu = val("m-cpu");
      m.memory_gb = val("m-memory"); m.disk_gb = val("m-disk"); m.username = val("m-user");
      m.network = val("m-network"); m.mode = val("m-mode"); m.ssh_key = val("m-key");
    } else if (screen === "firewall" && newRule) {
      for (const k of ["from", "action", "to", "cidr", "protocol", "ports", "description"]) newRule[k] = val("r-" + k);
    } else if (screen === "review") {
      const picked = document.querySelector("input[name=disk]:checked"); if (picked) s.disk = picked.value;
      const ok = document.getElementById("understood"); if (ok) s.understood = ok.checked;
    }
  }

  // --- checks ------------------------------------------------------------------

  function int(v, lo, hi) { const n = Number(v); return Number.isInteger(n) && n >= lo && n <= hi ? n : null; }

  function checkNetwork(n, others) {
    const e = {};
    if (!NAME_RE.test(n.name || "")) e.name = "Use lower-case letters, digits and dashes, starting with a letter.";
    else if (n.name === "internet") e.name = "'internet' is reserved.";
    else if (others.some((o) => o.name === n.name)) e.name = "Another network already has this name.";
    const net = cidr(n.cidr || "");
    if (!net) e.cidr = "Write it like 10.10.10.0/24.";
    else if (net.bad) e.cidr = "This range " + net.bad + ".";
    else if (net.prefix > 29) e.cidr = "Too small: use /29 or larger.";
    else if (net.prefix < 8) e.cidr = "Too big: use /8 or smaller.";
    else {
      const clash = others.find((o) => { const on = cidr(o.cidr); return on && !on.bad && overlaps(on, net); });
      if (clash) e.cidr = "Overlaps the " + clash.name + " network.";
      if (!usable(net, ip(n.router || ""))) e.router = "Must be an address inside " + n.cidr + ".";
      if (n.dhcp && n.dhcp.enabled) {
        const a = ip(n.dhcp.start || ""), b = ip(n.dhcp.end || ""), r = ip(n.router || "");
        if (!usable(net, a)) e.start = "Must be inside " + n.cidr + ".";
        else if (!usable(net, b)) e.end = "Must be inside " + n.cidr + ".";
        else if (a > b) e.end = "Must come after the first address.";
        else if (r !== null && a <= r && r <= b) e.start = "The range must not include the router address.";
      }
    }
    if (n.vlan !== "" && n.vlan !== undefined && n.vlan !== null) {
      const v = int(n.vlan, 1, 4093);
      if (v === null) e.vlan = "A number from 1 to 4093, or empty.";
      else if (others.some((o) => String(o.vlan) === String(v))) e.vlan = "Another network uses this VLAN.";
    } else if (others.some((o) => o.vlan === "" || o.vlan === undefined || o.vlan === null)) {
      e.vlan = "Only one network can go without a VLAN.";
    }
    return e;
  }

  function validate(name) {
    const e = {}, h = s.host;
    if (name === "host") {
      if (!NAME_RE.test(h.hostname)) e["h-hostname"] = "Lower-case letters, digits and dashes, starting with a letter.";
      if (!DOMAIN_RE.test(h.domain)) e["h-domain"] = "Like lab.internal.";
      if ((s.password || "").length < 8) e["h-password"] = "At least 8 characters.";
      if (h.mode === "static") {
        const a = /^([\d.]+)\/(\d{1,2})$/.exec(h.address);
        if (!a || ip(a[1]) === null || Number(a[2]) > 32) e["h-address"] = "Like 192.168.1.50/24.";
        if (ip(h.gateway) === null) e["h-gateway"] = "Like 192.168.1.1.";
        if (ip(h.dns) === null) e["h-dns"] = "Like 192.168.1.1.";
        if (!h.iface) e["h-iface"] = "Choose the network port.";
      }
    } else if (name === "network") {
      if (editNet) e.editing = "Save or cancel the network you are editing first.";
      s.networks.forEach((n, i) => {
        const bad = checkNetwork(n, s.networks.filter((_, j) => j !== i));
        if (Object.keys(bad).length) e["net-" + i] = n.name + ": " + Object.values(bad)[0];
      });
    } else if (name === "machines") {
      const m = s.machines;
      const count = int(m.count, 0, 50);
      if (count === null) e["m-count"] = "From 0 to 50.";
      if (!NAME_RE.test(m.name_prefix || "")) e["m-prefix"] = "Lower-case letters, digits and dashes.";
      if (int(m.cpu, 1, 64) === null) e["m-cpu"] = "From 1 to 64.";
      if (int(m.memory_gb, 1, 512) === null) e["m-memory"] = "At least 1.";
      if (int(m.disk_gb, 4, 10000) === null) e["m-disk"] = "At least 4.";
      if (!USER_RE.test(m.username || "") || m.username === "root") e["m-user"] = "Lower-case, not 'root'.";
      if (m.ssh_key && !KEY_RE.test(m.ssh_key)) e["m-key"] = "That does not look like an SSH public key.";
      const net = s.networks.find((n) => n.name === m.network);
      if (!net) e["m-network"] = "Choose a network.";
      else if (count && m.mode === "create") {
        if (!net.dhcp || !net.dhcp.enabled) e["m-network"] = "This network does not hand out addresses; turn that on first.";
        else {
          const size = ip(net.dhcp.end) - ip(net.dhcp.start) + 1;
          if (count > size) e["m-count"] = count + " machines do not fit in the " + size + " addresses of " + net.name + ".";
        }
      }
    } else if (name === "firewall") {
      if (newRule) e.rule = "Add or cancel the rule you are writing first.";
    } else if (name === "review") {
      if (!s.disk) e.disk = "Choose the disk to install on.";
      if (!s.understood) e.understood = "Tick the box to confirm.";
    }
    return e;
  }

  function checkRule(r) {
    if (!r.from) return "Choose the network the rule is for.";
    if (r.to === "cidr") {
      const c = cidr(r.cidr || "");
      if (!c || c.bad) return "Write the address range like 192.168.1.0/24.";
    }
    if (r.to === r.from) return "A network cannot have a rule to itself.";
    if (r.ports) {
      if (!["tcp", "udp"].includes(r.protocol)) return "Ports need TCP or UDP.";
      const ports = r.ports.split(",").map((p) => int(p.trim(), 1, 65535));
      if (ports.some((p) => p === null)) return "Ports are numbers from 1 to 65535, separated by commas.";
    }
    return null;
  }

  // --- the result ----------------------------------------------------------------

  function payload() {
    const h = s.host, m = s.machines;
    const host = { hostname: h.hostname, domain: h.domain, timezone: h.timezone, keyboard: h.keyboard, country: h.country,
      management: h.mode === "static"
        ? { mode: "static", address: h.address, gateway: h.gateway, dns: [h.dns], iface: h.iface }
        : { mode: "dhcp" } };
    const networks = s.networks.map((n) => {
      const out = { name: n.name, cidr: n.cidr, router: n.router, policy: n.policy || "isolated",
        dhcp: n.dhcp && n.dhcp.enabled ? { enabled: true, start: n.dhcp.start, end: n.dhcp.end } : { enabled: false } };
      if (n.vlan !== "" && n.vlan !== undefined && n.vlan !== null) out.vlan = Number(n.vlan);
      if (n.rules && n.rules.length) out.rules = n.rules;
      return out;
    });
    const machines = { count: Number(m.count), name_prefix: m.name_prefix, image: m.image || "alpine-3.22",
      cpu: Number(m.cpu), memory_mb: Number(m.memory_gb) * 1024, disk_gb: Number(m.disk_gb),
      network: m.network, username: m.username, mode: m.mode };
    if (m.ssh_key) machines.ssh_keys = [m.ssh_key];
    return { host, networks, machines, password: s.password, disk: s.disk };
  }

  // --- drawing ---------------------------------------------------------------------

  const field = (id, label, value, opts = {}) =>
    `<div class="f"><label for="${id}">${esc(label)}</label>` +
    `<input id="${id}" type="${opts.type || "text"}" value="${esc(value)}"${errors[id] ? ' class="bad"' : ""}${opts.placeholder ? ` placeholder="${esc(opts.placeholder)}"` : ""}>` +
    (errors[id] ? `<span class="err">${esc(errors[id])}</span>` : opts.hint ? `<span class="hint">${esc(opts.hint)}</span>` : "") + "</div>";

  const select = (id, label, value, options, opts = {}) =>
    `<div class="f"><label for="${id}">${esc(label)}</label><select id="${id}"${opts.change ? ` onchange="${opts.change}"` : ""}>` +
    options.map(([v, t]) => `<option value="${esc(v)}"${String(v) === String(value) ? " selected" : ""}>${esc(t)}</option>`).join("") +
    `</select>${errors[id] ? `<span class="err">${esc(errors[id])}</span>` : opts.hint ? `<span class="hint">${esc(opts.hint)}</span>` : ""}</div>`;

  function renderSteps() {
    const i = ORDER.indexOf(screen);
    $("#steps").innerHTML = i < 0 ? "" : STEPS.map((name, j) =>
      `<div class="step${j === i ? " on" : j < i ? " done" : ""}">${j + 1} &middot; ${name}</div>`).join("");
  }

  function nav(left, right) { $("#nav").innerHTML = (left || "<span></span>") + (right || ""); }

  const SCREENS = {
    loading() { nav(); return '<h1>Starting SNAP setup&hellip;</h1>'; },

    check() {
      nav('<button type="button" onclick="SNAP.poweroff()">Turn off computer</button>',
        '<button type="button" class="p" onclick="SNAP.recheck()">Check again</button>');
      return `<h1>This computer is not ready yet</h1>
        <p class="d">SNAP checked the hardware before touching any disk.</p>
        <div>${data.checks.map((c) => `<div class="k"><i class="${c.level}">${{ pass: "Pass", fix: "Fix", note: "Note" }[c.level]}</i>${esc(c.text)}</div>`).join("")}</div>
        <p class="d">Setup cannot continue until every Fix item is resolved. Notes can be ignored.</p>`;
    },

    host() {
      const h = s.host;
      nav("", '<button type="button" class="p" onclick="SNAP.next()">Next: Network</button>');
      const zones = (data.timezones || [h.timezone]).map((z) => [z, z]);
      const notes = (data.checks || []).filter((c) => c.level === "note");
      return `<h1>Your Proxmox computer</h1>
        <p class="d">Everything is filled in already. Change anything you like, or just press Next.</p>
        ${notes.map((c) => `<div class="x">${esc(c.text)}</div>`).join("")}
        <div class="r">${field("h-hostname", "Computer name", h.hostname)}${field("h-domain", "Domain", h.domain)}${select("h-timezone", "Time zone", h.timezone, zones)}</div>
        <div class="r">${select("h-keyboard", "Keyboard layout", h.keyboard, KEYBOARDS)}
          ${select("h-mode", "How this computer gets its address", h.mode, [["dhcp", "Automatically from your router (recommended)"], ["static", "Fixed address"]], { change: "SNAP.redraw()" })}
          <div class="f"></div></div>
        ${h.mode === "static" ? `<div class="r">${field("h-address", "Address", h.address, { placeholder: "192.168.1.50/24" })}${field("h-gateway", "Gateway", h.gateway, { placeholder: "192.168.1.1" })}${field("h-dns", "DNS server", h.dns, { placeholder: "192.168.1.1" })}
          ${select("h-iface", "Network port", h.iface, (data.interfaces || []).map((n) => [n.name, n.name + (n.carrier ? " (cable connected)" : "")]))}</div>` : ""}
        <div class="r" style="align-items:flex-end">${field("h-password", "Password for Proxmox, the router and the lab machines", s.password,
          { hint: "Made for you. Write it down, or type your own (8 characters or more)." })}
          <div class="f" style="flex:0 0 auto"><button type="button" onclick="SNAP.newPassword()">Make a new one</button></div></div>`;
    },

    network() {
      nav('<button type="button" onclick="SNAP.back()">Back</button>', '<button type="button" class="p" onclick="SNAP.next()">Next: Machines</button>');
      const list = s.networks.map((n, i) => editNet && editNet.index === i ? editPanel() : `
        <div class="item"><b>${esc(n.name)}</b><span class="tag">${esc(n.cidr)}</span><span class="tag">router ${esc(n.router)}</span>
          ${n.vlan !== "" && n.vlan !== undefined && n.vlan !== null ? `<span class="tag">VLAN ${esc(n.vlan)}</span>` : ""}
          <span class="tag">${n.dhcp && n.dhcp.enabled ? "addresses handed out automatically" : "fixed addresses only"}</span>
          <span class="end"><button type="button" class="sm" onclick="SNAP.editNetwork(${i})"${editNet ? " disabled" : ""}>Edit</button>
          ${s.networks.length > 1 ? `<button type="button" class="sm" onclick="SNAP.removeNetwork(${i})"${editNet ? " disabled" : ""}>Remove</button>` : ""}</span></div>
        ${errors["net-" + i] ? `<span class="err">${esc(errors["net-" + i])}</span>` : ""}`).join("");
      return `<h1>Your lab networks</h1>
        <p class="d">The lab gets its own private networks behind its own router and firewall. The defaults are safe if you are unsure.</p>
        ${list}${editNet && editNet.index === -1 ? editPanel() : ""}
        ${errors.editing ? `<span class="err">${esc(errors.editing)}</span>` : ""}
        <div><button type="button" onclick="SNAP.addNetwork()"${editNet ? " disabled" : ""}>+ Add another network</button></div>
        <p class="d">If one of these ranges is already used by your home network, SNAP moves it to a free range by itself and tells you.</p>`;
    },

    machines() {
      const m = s.machines;
      nav('<button type="button" onclick="SNAP.back()">Back</button>', '<button type="button" class="p" onclick="SNAP.next()">Next: Firewall</button>');
      return `<h1>Lab machines</h1><p class="d">How many Linux machines (Alpine Linux, small and quick) to create, and how big each one is.</p>
        <div class="r">${field("m-count", "Number of machines", m.count)}
          ${select("m-image", "Operating system", "alpine-3.22", [["alpine-3.22", "Alpine Linux 3.22 (on the stick)"]])}
          ${field("m-prefix", "Name", m.name_prefix, { hint: `Machines are named ${m.name_prefix || "name"}-01, ${m.name_prefix || "name"}-02, ...` })}</div>
        <div class="r">${field("m-cpu", "Processors each", m.cpu)}${field("m-memory", "Memory each (GB)", m.memory_gb)}${field("m-disk", "Disk each (GB)", m.disk_gb)}</div>
        <div class="r">${field("m-user", "User name", m.username, { hint: "Signs in with the password from step 1." })}
          ${select("m-network", "Network", m.network, s.networks.map((n) => [n.name, n.name]))}
          ${select("m-mode", "When", m.mode, [["create", "Create them now"], ["templates-only", "Only prepare the template"]])}</div>
        <div class="r">${field("m-key", "SSH public key (optional)", m.ssh_key || "", { placeholder: "ssh-ed25519 AAAA..." })}</div>`;
    },

    firewall() {
      nav('<button type="button" onclick="SNAP.back()">Back</button>', '<button type="button" class="p" onclick="SNAP.next()">Next: Review</button>');
      const rows = s.networks.map((n, i) => `<div class="r" style="align-items:center"><b style="width:90px">${esc(n.name)}</b><div class="cards">` +
        POLICIES.map(([p, t, d]) => `<button type="button" class="card${n.policy === p ? " on" : ""}" aria-pressed="${n.policy === p}" onclick="SNAP.policy(${i},'${p}')"><b>${t}</b>${d}</button>`).join("") +
        "</div></div>").join("");
      const rules = [];
      s.networks.forEach((n, i) => (n.rules || []).forEach((r, j) => rules.push(
        `<div class="k"><i>${r.action === "allow" ? "Allow" : "Block"}</i><span style="flex:1">${esc(n.name)} to ${esc(r.to)}${r.protocol && r.protocol !== "any" ? ", " + r.protocol.toUpperCase() : ""}${r.ports ? " port " + r.ports.join(", ") : ""}${r.description ? " &middot; " + esc(r.description) : ""}</span>
         <button type="button" class="sm" onclick="SNAP.removeRule(${i},${j})">Remove</button></div>`)));
      return `<h1>Who can talk to whom</h1>
        <p class="d">Choose what each network may reach. Nothing in the lab can ever reach your Proxmox computer.</p>
        ${rows}
        <div>${rules.join("")}</div>
        ${newRule ? rulePanel() : '<div><button type="button" onclick="SNAP.addRule()">+ Add rule</button></div>'}
        ${errors.rule ? `<span class="err">${esc(errors.rule)}</span>` : ""}`;
    },

    review() {
      const p = payload(), m = p.machines;
      nav('<button type="button" onclick="SNAP.back()">Back</button>', '<button type="button" class="p" onclick="SNAP.start()">Start installation</button>');
      const disks = (data.disks || []).map((d) => `
        <div class="item${s.disk === d.name ? " on" : ""}"><input id="disk-${esc(d.name)}" name="disk" type="radio" value="${esc(d.name)}"${s.disk === d.name ? " checked" : ""} onchange="SNAP.redraw()">
        <label for="disk-${esc(d.name)}">${esc(d.model || "Disk")}, ${esc(d.gb)} GB <span class="hint">(${esc(d.name)})</span></label></div>`).join("");
      return `<h1>Check and install</h1>
        <div class="r"><div style="flex:1">
          <div class="k"><i>Computer</i>${esc(p.host.hostname)}.${esc(p.host.domain)}, ${esc(p.host.timezone)}</div>
          <div class="k"><i>Networks</i>${p.networks.map((n) => esc(n.name + " " + n.cidr)).join(", ")}</div>
          <div class="k"><i>Machines</i>${m.mode === "create" ? `${m.count} x Alpine Linux 3.22, ${m.cpu} CPU, ${m.memory_mb / 1024} GB, ${m.disk_gb} GB, on ${esc(m.network)}` : "template only"}</div>
          <div class="k"><i>Firewall</i>${p.networks.map((n) => esc(n.name + ": " + POLICIES.find((x) => x[0] === n.policy)[1].toLowerCase())).join(". ")}</div>
          <div class="k"><i>Password</i><span class="pw" style="font-size:16px">${esc(s.password)}</span></div>
        </div><div style="flex:1;display:flex;flex-direction:column;gap:10px">
          <span class="lbl">Install on this disk</span>
          ${disks || '<div class="x">No internal disk was found.</div>'}
          ${errors.disk ? `<span class="err">${esc(errors.disk)}</span>` : ""}
          <span class="hint">USB drives, including this stick, are never shown.</span>
          <div class="x"><b>Everything on the chosen disk will be erased.</b></div>
          <div><input id="understood" type="checkbox"${s.understood ? " checked" : ""}> <label for="understood">I understand; erase it and install</label></div>
          ${errors.understood ? `<span class="err">${esc(errors.understood)}</span>` : ""}
        </div></div>`;
    },

    installing() {
      nav('<button type="button" onclick="SNAP.toggleLog()">Show technical details</button>', '<span class="d">Do not turn off the computer.</span>');
      return `<h1>Installing Proxmox</h1>
        <p class="d">This takes about 10 minutes. The computer then restarts by itself and SNAP builds your lab. Leave the USB stick in.</p>
        <div style="display:flex;justify-content:space-between"><b id="p-text">Getting ready</b><span id="p-pct">0%</span></div>
        <div class="bar"><i id="p-bar"></i></div>
        <div class="r"><div class="card"><span class="d">Your password</span><div class="pw" style="margin-top:6px">${esc(s.password)}</div><span class="hint">Write it down now. It is shown again when the lab is ready.</span></div>
          <div class="card"><span class="d">Next</span><div style="margin-top:6px">Restart, then CRACKLE prepares the computer and POP builds the router, networks and machines (about 40 minutes).</div></div></div>
        <div id="log" class="log hidden">${esc(installLog.join("\n"))}</div>`;
    },

    failed() {
      nav('<button type="button" onclick="SNAP.poweroff()">Turn off computer</button>', '<button type="button" class="p" onclick="SNAP.retry()">Try again</button>');
      return `<h1>The installation stopped</h1>
        <p class="d">Nothing else was changed. Check the message, then try again.</p>
        <div class="x">${esc(s.failure || "Unknown error")}</div>
        <div class="log">${esc(installLog.slice(-30).join("\n"))}</div>`;
    },

    done() {
      nav("", '<span class="d">Restarting&hellip;</span>');
      return `<h1>Proxmox is installed</h1>
        <p class="d">The computer restarts now and SNAP continues building your lab. Leave the USB stick in; the screen shows the progress.</p>
        <div class="card" style="flex:none"><span class="d">Your password</span><div class="pw" style="margin-top:6px">${esc(s.password)}</div></div>`;
    },
  };

  function editPanel() {
    const n = editNet.net;
    // Problems found by Save show under their fields (field() reads errors["n-..."]).
    errors = Object.assign({}, errors, Object.fromEntries(Object.entries(editNet.errors || {}).map(([k, v]) => ["n-" + k, v])));
    const f = (id, label, value, key, hint) => field(id, label, value, { hint });
    return `<div class="panel"><b>${editNet.index === -1 ? "New network" : "Edit " + esc(n.name)}</b>
      <div class="r">${f("n-name", "Name", n.name, "name")}${f("n-cidr", "Address range", n.cidr, "cidr", "Like 10.10.30.0/24")}${f("n-router", "Router address", n.router, "router")}</div>
      <div class="r">${f("n-vlan", "VLAN (optional)", n.vlan ?? "", "vlan", "1 to 4093")}${f("n-start", "First automatic address", (n.dhcp || {}).start || "", "start")}${f("n-end", "Last automatic address", (n.dhcp || {}).end || "", "end")}</div>
      <div><input id="n-dhcp" type="checkbox"${n.dhcp && n.dhcp.enabled ? " checked" : ""}> <label for="n-dhcp">Hand out addresses automatically (DHCP)</label></div>
      <div style="display:flex;gap:8px"><button type="button" class="p sm" onclick="SNAP.saveNetwork()">Save network</button><button type="button" class="sm" onclick="SNAP.cancelNetwork()">Cancel</button></div></div>`;
  }

  function rulePanel() {
    const r = newRule;
    const targets = [...s.networks.map((n) => [n.name, n.name]), ["internet", "the internet"], ["cidr", "an address range..."]];
    return `<div class="panel"><b>New rule</b>
      <div class="r">${select("r-from", "For network", r.from, s.networks.map((n) => [n.name, n.name]))}
        ${select("r-action", "Action", r.action, [["allow", "Allow"], ["block", "Block"]])}
        ${select("r-to", "To", r.to, targets, { change: "SNAP.redraw()" })}
        ${r.to === "cidr" ? field("r-cidr", "Address range", r.cidr || "", { placeholder: "192.168.1.0/24" }) : ""}</div>
      <div class="r">${select("r-protocol", "Protocol", r.protocol, [["any", "Any"], ["tcp", "TCP"], ["udp", "UDP"], ["icmp", "ICMP (ping)"]])}
        ${field("r-ports", "Ports (TCP or UDP only)", r.ports || "", { placeholder: "22, 443" })}
        ${field("r-description", "Description (optional)", r.description || "")}</div>
      ${r.error ? `<span class="err">${esc(r.error)}</span>` : ""}
      <div style="display:flex;gap:8px"><button type="button" class="p sm" onclick="SNAP.saveRule()">Add rule</button><button type="button" class="sm" onclick="SNAP.cancelRule()">Cancel</button></div></div>`;
  }

  function render() {
    renderSteps();
    $("#screen").innerHTML = SCREENS[screen]();
    const first = document.querySelector("#screen .bad, #screen input, #screen select");
    if (first && screen !== "installing") first.focus();
  }

  // --- actions ------------------------------------------------------------------------

  function nextFreeNetwork() {
    for (let k = 30; k < 255; k += 10) {
      const c = `10.10.${k}.0/24`, net = cidr(c);
      if (s.networks.some((n) => { const o = cidr(n.cidr); return o && !o.bad && overlaps(o, net); })) continue;
      const vlans = s.networks.map((n) => Number(n.vlan));
      let vlan = k; while (vlans.includes(vlan)) vlan++;
      return { name: "", cidr: c, router: `10.10.${k}.1`, vlan, policy: "isolated",
        dhcp: { enabled: true, start: `10.10.${k}.100`, end: `10.10.${k}.199` }, rules: [] };
    }
    return { name: "", cidr: "", router: "", vlan: "", policy: "isolated", dhcp: { enabled: true, start: "", end: "" }, rules: [] };
  }

  return {
    init, next, back, start,
    redraw() { readForm(); render(); },
    newPassword() { readForm(); s.password = makePassword(); render(); },
    editNetwork(i) { readForm(); errors = {}; editNet = { index: i, net: clone(s.networks[i]) }; render(); },
    addNetwork() { readForm(); errors = {}; editNet = { index: -1, net: nextFreeNetwork() }; render(); },
    cancelNetwork() { editNet = null; errors = {}; render(); },
    saveNetwork() {
      readForm();
      const others = s.networks.filter((_, j) => j !== editNet.index);
      const e = checkNetwork(editNet.net, others);
      errors = {};
      if (Object.keys(e).length) { editNet.errors = e; render(); editNet.errors = {}; return; }
      const old = editNet.index >= 0 ? s.networks[editNet.index].name : null;
      if (editNet.index === -1) s.networks.push(editNet.net); else s.networks[editNet.index] = editNet.net;
      if (old && old !== editNet.net.name) {
        if (s.machines.network === old) s.machines.network = editNet.net.name;
        s.networks.forEach((n) => (n.rules || []).forEach((r) => { if (r.to === old) r.to = editNet.net.name; }));
      }
      editNet = null; render();
    },
    removeNetwork(i) {
      readForm();
      const gone = s.networks[i].name;
      s.networks.splice(i, 1);
      s.networks.forEach((n) => { n.rules = (n.rules || []).filter((r) => r.to !== gone); });
      if (s.machines.network === gone) s.machines.network = s.networks[0].name;
      render();
    },
    policy(i, p) { s.networks[i].policy = p; render(); },
    addRule() { newRule = { from: s.networks[0].name, action: "allow", to: "internet", protocol: "any", ports: "", description: "" }; errors = {}; render(); },
    cancelRule() { newRule = null; errors = {}; render(); },
    saveRule() {
      readForm();
      const err = checkRule(newRule);
      if (err) { newRule.error = err; render(); return; }
      const r = { action: newRule.action, to: newRule.to === "cidr" ? newRule.cidr : newRule.to, protocol: newRule.protocol };
      if (newRule.ports) r.ports = newRule.ports.split(",").map((p) => Number(p.trim()));
      if (newRule.description) r.description = newRule.description;
      const n = s.networks.find((x) => x.name === newRule.from);
      n.rules = [...(n.rules || []), r];
      newRule = null; errors = {}; render();
    },
    removeRule(i, j) { s.networks[i].rules.splice(j, 1); render(); },
    toggleLog() { const l = $("#log"); if (l) l.classList.toggle("hidden"); },
    retry() { start(); },
    recheck() { send("recheck"); },
    poweroff() { send("poweroff"); },

    // From the window.
    progress(pct, text, line) {
      if (line) installLog.push(line);
      if (screen !== "installing") return;
      if (pct !== null && pct !== undefined) {
        $("#p-bar").style.width = Math.max(0, Math.min(100, pct)) + "%";
        $("#p-pct").textContent = Math.round(pct) + "%";
      }
      if (text) $("#p-text").textContent = text;
      const log = $("#log"); if (log) { log.textContent = installLog.join("\n"); log.scrollTop = log.scrollHeight; }
    },
    installFailed(message) { s.failure = message; screen = "failed"; render(); },
    installDone() { screen = "done"; render(); },
    // For tests.
    state: () => ({ screen, settings: s, errors, payload: s && payload() }),
  };
})();

window.SNAP = SNAP;
window.addEventListener("DOMContentLoaded", () => {
  if (window.SNAP_TEST_DATA) SNAP.init(window.SNAP_TEST_DATA);
  else location.href = "snap:ready";
});
