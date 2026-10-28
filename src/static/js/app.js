const $ = (id) => document.getElementById(id);

function toast(message, kind = "success") {
    const el = document.createElement("div");
    el.className = "toast toast-" + kind;
    el.textContent = message;
    $("toasts").appendChild(el);
    setTimeout(() => el.remove(), 4000);
}

function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    Object.entries(attrs).forEach(([key, value]) => {
        if (key === "class") node.className = value;
        else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
        else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value === true ? "" : value);
    });
    node.append(...children.filter((child) => child !== null && child !== undefined));
    return node;
}

async function getJson(url, options = {}) {
    const response = await fetch(url, {headers: {Accept: "application/json", ...(options.body ? {"Content-Type": "application/json"} : {})}, ...options});
    const data = await response.json().catch(() => ({}));
    if (!response.ok && !("ok" in data)) throw new Error(data.error || `Erro HTTP ${response.status}`);
    return data;
}

// ---------- Spinner ao mudar de página ou guardar ----------
function showBusy(text) {
    $("page-busy-text").textContent = text || "A carregar…";
    $("page-busy").hidden = false;
}
const hideBusy = () => { $("page-busy").hidden = true; };
document.addEventListener("click", (event) => {
    const link = event.target.closest("a[href]");
    if (!link || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const url = new URL(link.href, location.href);
    // Downloads não mudam de página
    if (url.origin !== location.origin || link.target || /\/ficheiro$|\/imagem$/.test(url.pathname)
        || (url.pathname === location.pathname && url.search === location.search)) return;
    showBusy(link.dataset.busyText);
});
// Confirmação antes de ações destrutivas; depois o spinner
document.addEventListener("submit", (event) => {
    const form = event.target;
    if (form.dataset.confirm && !confirm(form.dataset.confirm)) {
        event.preventDefault();
        return;
    }
    if (!event.defaultPrevented && form.method.toLowerCase() === "post") showBusy();
});
window.addEventListener("pageshow", hideBusy);
document.querySelectorAll("[data-flash]").forEach((node) => setTimeout(() => node.remove(), 4000));

// ---------- Lista de reports: filtro de categorias ("Todas" exclui as outras; aplica ao fechar) ----------
const catFilter = document.querySelector(".cat-filter");
if (catFilter) {
    const dropdown = catFilter.querySelector("details");
    const all = catFilter.querySelector("[data-all]");
    const boxes = [...catFilter.querySelectorAll("input[name=categoria]")];
    const initial = new FormData(catFilter).getAll("categoria").join("\n");
    catFilter.addEventListener("change", (event) => {
        if (event.target === all) boxes.forEach((box) => { box.checked = false; });
        all.checked = !boxes.some((box) => box.checked);
    });
    catFilter.addEventListener("submit", () => showBusy());
    dropdown.addEventListener("toggle", () => {
        if (!dropdown.open && new FormData(catFilter).getAll("categoria").join("\n") !== initial) catFilter.requestSubmit();
    });
    document.addEventListener("click", (event) => { if (dropdown.open && !dropdown.contains(event.target)) dropdown.open = false; });
    document.addEventListener("keydown", (event) => { if (event.key === "Escape") dropdown.open = false; });
}

// ---------- Admin: testar fonte e canal ----------
document.querySelectorAll("[data-test-url]").forEach((button) => button.addEventListener("click", async () => {
    showBusy("A testar a ligação…");
    try {
        const data = await getJson(button.dataset.testUrl, {method: "POST"});
        toast(data.message, data.ok ? "success" : "error");
    } catch (error) {
        toast(error.message, "error");
    } finally {
        hideBusy();
    }
}));

const channelTest = $("channel-test");
if (channelTest) channelTest.addEventListener("submit", async (event) => {
    event.preventDefault();
    showBusy("A enviar o teste…");
    try {
        const data = await getJson(channelTest.dataset.url, {method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(channelTest)))});
        toast(data.message, data.ok ? "success" : "error");
    } catch (error) {
        toast(error.message, "error");
    } finally {
        hideBusy();
    }
});

// ---------- Report: variáveis inseridas no assunto ou na mensagem (o último que teve o foco) ----------
const varChips = $("var-chips");
if (varChips) {
    let target = document.querySelector("[name=message]");
    document.querySelectorAll("[name=subject], [name=message]").forEach((field) => field.addEventListener("focus", () => { target = field; }));
    varChips.addEventListener("click", (event) => {
        const chip = event.target.closest("[data-var]");
        if (!chip) return;
        target.setRangeText(chip.dataset.var, target.selectionStart, target.selectionEnd, "end");
        target.focus();
    });
}

// ---------- Assinaturas: editor num iframe sem scripts (o HTML colado não corre aqui) ----------
const sigEditor = $("sig-editor");
if (sigEditor) {
    const source = $("sig-html");
    const doc = sigEditor.contentDocument;
    doc.open();
    doc.write('<!doctype html><html><body style="margin:12px;font-family:Arial,sans-serif;font-size:14px"></body></html>');
    doc.close();
    doc.body.innerHTML = source.value;
    doc.designMode = "on";
    let raw = false;
    const insertImage = (file) => {
        if (!file || !file.type.startsWith("image/")) return;
        const reader = new FileReader();
        reader.onload = () => doc.execCommand("insertImage", false, reader.result);
        reader.readAsDataURL(file);
    };
    // Uma imagem colada (print, cópia de imagem) fica embebida
    doc.addEventListener("paste", (event) => {
        const file = [...event.clipboardData.files].find((item) => item.type.startsWith("image/"));
        if (file) {
            event.preventDefault();
            insertImage(file);
        }
    });
    $("sig-image").addEventListener("change", (event) => {
        insertImage(event.target.files[0]);
        event.target.value = "";
    });
    document.querySelectorAll("[data-cmd]").forEach((button) => button.addEventListener("click", () => {
        const url = button.dataset.cmd === "createLink" && prompt("Endereço da ligação", "https://");
        if (button.dataset.cmd !== "createLink" || url) doc.execCommand(button.dataset.cmd, false, url || null);
        sigEditor.contentWindow.focus();
    }));
    $("sig-source").addEventListener("click", (event) => {
        raw = !raw;
        if (raw) source.value = doc.body.innerHTML;
        else doc.body.innerHTML = source.value;
        source.hidden = !raw;
        sigEditor.hidden = raw;
        event.currentTarget.setAttribute("aria-pressed", raw);
    });
    $("signature-form").addEventListener("submit", () => {
        if (!raw) source.value = doc.body.innerHTML;
    });
}

// ---------- Configurações: pré-visualização da cor (só é guardada com "Guardar") ----------
const accentInput = $("accentInput");
if (accentInput) {
    const swatches = document.querySelectorAll("[data-swatch]");
    const darker = (hex) => "#" + [1, 3, 5].map((i) => Math.floor(parseInt(hex.slice(i, i + 2), 16) * .85).toString(16).padStart(2, "0")).join("");
    function hexToHsl(hex) {
        const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
        const max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2, d = max - min;
        let h = 0, s = 0;
        if (d) {
            s = d / (1 - Math.abs(2 * l - 1));
            h = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
        }
        return `${((h * 60 + 360) % 360).toFixed(1)} ${(s * 100).toFixed(1)}% ${(l * 100).toFixed(1)}%`;
    }
    function previewAccent(color) {
        accentInput.value = color;
        const root = document.documentElement.style;
        root.setProperty("--brand-from", color);
        root.setProperty("--brand-to", darker(color));
        root.setProperty("--primary", hexToHsl(color));
        root.setProperty("--ring", hexToHsl(color));
        swatches.forEach((swatch) => swatch.setAttribute("aria-pressed", swatch.dataset.swatch === color));
    }
    swatches.forEach((swatch) => swatch.addEventListener("click", () => previewAccent(swatch.dataset.swatch)));
    accentInput.addEventListener("input", () => previewAccent(accentInput.value));
    swatches.forEach((swatch) => swatch.setAttribute("aria-pressed", swatch.dataset.swatch === (accentInput.dataset.saved || "#2563eb")));
}

// ---------- Execução: atualiza enquanto está pendente ----------
const runStatus = $("run-status");
if (runStatus && ["pendente", "a executar"].includes(runStatus.dataset.status)) {
    const timer = setInterval(async () => {
        try {
            const data = await getJson($("run-log").dataset.url);
            if (!["pendente", "a executar"].includes(data.status)) {
                clearInterval(timer);
                location.reload();
            }
        } catch (error) { /* tenta de novo */ }
    }, 3000);
}

// ---------- Editor do report ----------
const reportData = $("report-data") && JSON.parse($("report-data").textContent);
if (reportData) {
    let panels = reportData.panels;              // [{id, title}] pela ordem do report
    let variables = reportData.variables;        // {nome: valor}
    let available = [];                          // painéis do dashboard
    const sourceSelect = $("f-source");
    const dashboardsUrl = (path = "") => `${reportData.dashboardsUrl}/${sourceSelect.value}/dashboards${path}`;

    function renderSelected() {
        const list = $("panels-selected");
        list.replaceChildren(...panels.map((panel, index) => el("li", {},
            el("span", {}, panel.title, " ", el("small", {class: "muted"}, `#${panel.id}`)),
            el("select", {class: "panel-period", title: "Período deste painel", onchange: (event) => { panel.period = event.target.value || null; }},
                el("option", {value: ""}, "Período do report"),
                ...Object.entries(reportData.periods).map(([key, label]) => el("option", {value: key, selected: panel.period === key}, label))),
            el("button", {type: "button", class: "icon-mini", title: "Subir", disabled: index === 0, onclick: () => move(index, -1)}, "↑"),
            el("button", {type: "button", class: "icon-mini", title: "Descer", disabled: index === panels.length - 1, onclick: () => move(index, 1)}, "↓"),
            el("button", {type: "button", class: "icon-mini", title: "Tirar", onclick: () => toggle(panel, false)}, "✕"),
        )));
        $("panels-count").textContent = panels.length;
        document.querySelectorAll("#panels-available input").forEach((input) => {
            input.checked = panels.some((panel) => panel.id === Number(input.value));
        });
    }
    function move(index, delta) {
        const [panel] = panels.splice(index, 1);
        panels.splice(index + delta, 0, panel);
        renderSelected();
    }
    function toggle(panel, on) {
        panels = panels.filter((item) => item.id !== panel.id);
        if (on) panels.push({id: panel.id, title: panel.title, period: null});
        renderSelected();
    }
    function renderAvailable() {
        const list = $("panels-available");
        if (!available.length) {
            list.replaceChildren(el("li", {class: "muted"}, "Sem painéis neste dashboard."));
            return;
        }
        list.replaceChildren(...available.map((panel) => el("li", {},
            el("label", {class: "switch-row"},
                el("input", {type: "checkbox", value: panel.id, onchange: (event) => toggle(panel, event.target.checked)}),
                el("span", {}, panel.title, " ", el("small", {class: "muted"}, `#${panel.id} · ${panel.type}`))))));
        renderSelected();
    }
    function renderVariables(defs) {
        const box = $("variables");
        // Variáveis guardadas que o dashboard já não tem também aparecem, para não se perderem sem aviso
        const names = [...new Set([...defs.map((d) => d.name), ...Object.keys(variables)])];
        $("variables-box").hidden = !names.length;
        box.replaceChildren(...names.map((name) => {
            const def = defs.find((d) => d.name === name) || {label: name, options: [], current: ""};
            const listId = `var-${name}`;
            return el("label", {class: "field"}, el("span", {class: "label"}, def.label, " ", el("small", {class: "muted"}, `var-${name}`)),
                el("input", {value: variables[name] ?? "", placeholder: def.current || "(valor do dashboard)", list: listId,
                    oninput: (event) => {
                        if (event.target.value) variables[name] = event.target.value; else delete variables[name];
                    }}),
                el("datalist", {id: listId}, ...def.options.map((option) => el("option", {value: option}))));
        }));
    }
    async function loadDashboard(uid) {
        $("panels-available").replaceChildren(el("li", {class: "muted"}, "A carregar painéis…"));
        try {
            const data = await getJson(dashboardsUrl(`/${encodeURIComponent(uid)}`));
            available = data.panels;
            // Os títulos guardados podem estar desatualizados (ou ser "Painel N", na importação)
            panels.forEach((panel) => {
                const current = available.find((item) => item.id === panel.id);
                if (current) panel.title = current.title;
            });
            $("f-uid").value = data.uid;
            $("f-slug").value = data.slug;
            $("f-title").value = data.title;
            $("dash-current").replaceChildren(el("strong", {}, data.title), " ", el("small", {}, `(${data.uid})`));
            renderAvailable();
            renderVariables(data.variables);
        } catch (error) {
            $("panels-available").replaceChildren(el("li", {class: "op-error"}, error.message));
            renderVariables([]);
        }
    }
    async function searchDashboards() {
        const results = $("dash-results");
        results.hidden = false;
        results.replaceChildren(el("li", {class: "muted"}, "A procurar…"));
        try {
            const rows = await getJson(dashboardsUrl(`?q=${encodeURIComponent($("dash-search").value)}`));
            results.replaceChildren(...(rows.length ? rows.map((row) => el("li", {},
                el("button", {type: "button", class: "pick", onclick: () => {
                    results.hidden = true;
                    if (row.uid !== $("f-uid").value) {
                        panels = [];
                        variables = {};
                    }
                    loadDashboard(row.uid);
                }}, el("strong", {}, row.title), " ", el("small", {class: "muted"}, row.folder)))) : [el("li", {class: "muted"}, "Nenhum dashboard encontrado.")]));
        } catch (error) {
            results.replaceChildren(el("li", {class: "op-error"}, error.message));
        }
    }
    $("dash-search-btn").addEventListener("click", searchDashboards);
    $("dash-search").addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            searchDashboards();
        }
    });
    sourceSelect.addEventListener("change", () => { $("dash-results").hidden = true; });

    // Destinatários
    const channelById = Object.fromEntries(reportData.channels.map((c) => [String(c.id), c]));
    function recipientRow(item = {}) {
        const row = el("div", {class: "recipient"});
        const select = el("select", {"data-r": "channel_id"}, ...reportData.channels.map((c) =>
            el("option", {value: c.id, selected: String(c.id) === String(item.channel_id)}, c.name)));
        function fields() {
            const telegram = channelById[select.value]?.kind === "telegram";
            row.replaceChildren(
                el("label", {class: "field"}, el("span", {class: "label"}, "Canal"), select),
                telegram
                    ? el("label", {class: "field"}, el("span", {class: "label"}, "chat_id"), el("input", {"data-r": "chat_id", value: item.chat_id || "", placeholder: "-1001234567890 ou @canal"}))
                    : el("label", {class: "field"}, el("span", {class: "label"}, "Para"), el("input", {"data-r": "to", value: (item.to || []).join ? (item.to || []).join(", ") : item.to || "", placeholder: "a@multicert.com, b@multicert.com"})),
                telegram
                    ? el("label", {class: "field"}, el("span", {class: "label"}, "ID do tópico (opcional)"), el("input", {"data-r": "topic_id", value: item.topic_id || "", placeholder: "42"}))
                    : el("label", {class: "field"}, el("span", {class: "label"}, "Cc (opcional)"), el("input", {"data-r": "cc", value: (item.cc || []).join ? (item.cc || []).join(", ") : item.cc || ""})),
                el("button", {type: "button", class: "btn btn-outline btn-sm", onclick: () => row.remove()}, "Remover"));
        }
        select.addEventListener("change", () => { item = {}; fields(); });
        fields();
        return row;
    }
    reportData.recipients.forEach((item) => $("recipients").append(recipientRow(item)));
    $("add-recipient").addEventListener("click", () => $("recipients").append(recipientRow()));

    // Consultas SQL: folha, ligação e query, por ordem
    function queryRow(item = {}) {
        const row = el("div", {class: "query"});
        const up = el("button", {type: "button", class: "btn btn-outline btn-sm", title: "Subir", onclick: () => row.previousElementSibling?.before(row)}, "↑");
        const down = el("button", {type: "button", class: "btn btn-outline btn-sm", title: "Descer", onclick: () => row.nextElementSibling?.after(row)}, "↓");
        row.append(
            el("div", {class: "field-row-2"},
                el("label", {class: "field"}, el("span", {class: "label"}, "Folha"), el("input", {"data-q": "sheet", value: item.sheet || "", maxlength: 31, placeholder: "UTILIZADORES"})),
                el("label", {class: "field"}, el("span", {class: "label"}, "Ligação"), el("select", {"data-q": "connection_id"}, ...reportData.connections.map((c) =>
                    el("option", {value: c.id, selected: String(c.id) === String(item.connection_id)}, `${c.name} (${c.agent_name})`))))),
            el("label", {class: "field"}, el("span", {class: "label"}, "Query"), el("textarea", {"data-q": "query", class: "code-input", rows: 8, spellcheck: "false"}, item.query || "")),
            el("div", {class: "row-actions"}, up, down, el("button", {type: "button", class: "btn btn-outline btn-sm", onclick: () => row.remove()}, "Remover")));
        return row;
    }
    if (reportData.kind === "sql") reportData.queries.forEach((item) => $("queries").append(queryRow(item)));
    $("add-query").addEventListener("click", () => $("queries").append(queryRow()));

    // Tipo do report: mostra só os blocos desse tipo (os escondidos não vão no POST)
    function syncKind() {
        const kind = $("f-kind").value;
        document.querySelectorAll("#report-form [data-kind]").forEach((node) => {
            const on = node.dataset.kind.split(" ").includes(kind);
            node.hidden = !on;
            if (node.tagName === "OPTION") {
                // Opção só de alguns tipos (ex. "Sem período"): se estava escolhida, volta à primeira
                if (!on && node.selected) node.parentElement.selectedIndex = 0;
                node.disabled = !on;
            }
            node.querySelectorAll("input, select, textarea").forEach((input) => { input.disabled = !on; });
        });
        if (kind === "sql" && !$("queries").children.length && reportData.connections.length) $("queries").append(queryRow());
    }
    $("f-kind").addEventListener("change", () => { syncKind(); syncForm(); });
    syncKind();

    // Agendamento e formato: mostra só os campos que se aplicam
    function syncForm() {
        const freq = $("f-freq").value;
        document.querySelectorAll(".sched").forEach((node) => { node.hidden = !freq; });
        document.querySelector(".sched-weekly").hidden = freq !== "weekly";
        document.querySelector(".sched-monthly").hidden = freq !== "monthly";
        document.querySelectorAll(".pdf-only").forEach((node) => { node.hidden = $("f-output").disabled || $("f-output").value !== "pdf"; });
    }
    $("f-freq").addEventListener("change", syncForm);
    $("f-output").addEventListener("change", syncForm);
    syncForm();

    $("report-form").addEventListener("submit", () => {
        $("f-panels").value = JSON.stringify(panels);
        $("f-variables").value = JSON.stringify(variables);
        $("f-recipients").value = JSON.stringify([...document.querySelectorAll(".recipient")].map((row) =>
            Object.fromEntries([...row.querySelectorAll("[data-r]")].map((input) => [input.dataset.r, input.value]))));
        const kind = $("f-kind").value;
        $("f-queries").value = JSON.stringify(kind === "sql"
            ? [...document.querySelectorAll(".query")].map((row) => Object.fromEntries([...row.querySelectorAll("[data-q]")].map((input) => [input.dataset.q, input.value])))
            : kind === "script" ? [...document.querySelectorAll("[data-script-connection]:checked")].map((box) => ({connection_id: box.value})) : []);
    });

    renderSelected();
    renderVariables([]);
    if ($("f-uid").value && sourceSelect.value) loadDashboard($("f-uid").value);

    // Pré-visualização depois de "Guardar e pré-visualizar"
    if (reportData.previewUrl && reportData.autoPreview) {
        (async () => {
            $("preview").hidden = false;
            $("preview").scrollIntoView({behavior: "smooth"});
            showBusy("A gerar a pré-visualização (render dos painéis no Grafana)…");
            try {
                const response = await fetch(reportData.previewUrl, {method: "POST"});
                if (!response.ok) {
                    const data = await response.json().catch(() => ({}));
                    $("preview-log").hidden = false;
                    $("preview-log").replaceChildren(...(data.log || []).map((line) => el("li", {class: `op-${line.level}`}, el("time", {}, line.time), line.message)),
                        el("li", {class: "op-error"}, data.error || `Erro HTTP ${response.status}`));
                    return;
                }
                const blob = await response.blob();
                const url = URL.createObjectURL(blob);
                const name = (response.headers.get("Content-Disposition") || "").split("filename=")[1] || "report";
                Object.assign($("preview-download"), {href: url, download: name, hidden: false});
                if (blob.type === "application/pdf") Object.assign($("preview-frame"), {src: url, hidden: false});
            } catch (error) {
                toast(error.message, "error");
            } finally {
                hideBusy();
            }
        })();
    }
}

$("themeToggle").addEventListener("click", () => {
    const dark = document.documentElement.classList.toggle("dark");
    try { localStorage.setItem("theme", dark ? "dark" : "light"); } catch (e) {}
});
$("menuToggle").addEventListener("click", () => $("sidebar").classList.add("open"));
$("sidebarOverlay").addEventListener("click", () => $("sidebar").classList.remove("open"));
$("year").textContent = new Date().getFullYear();
