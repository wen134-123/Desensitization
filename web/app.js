/* 标书脱敏工作台 · 前端交互 v2 */
(function () {
  "use strict";

  var state = {
    file: null,
    analysis: null,
    jobId: null,
    preview: null,
    previewTimer: null,
    previewSeq: 0,
    previewController: null,
    previewPage: 0,
    step: 1,
    result: null,
    analyzing: false,
    redacting: false,
    restoring: false
  };

  var CATEGORY_ORDER = [
    "COMPANY", "PERSON", "PHONE", "IDCARD", "BANK",
    "EMAIL", "ADDRESS", "AMOUNT", "CODE", "CREDITCODE"
  ];

  var ALL_CATEGORIES = [
    { key: "COMPANY", cn: "公司/单位" },
    { key: "PERSON", cn: "人名" },
    { key: "PHONE", cn: "电话" },
    { key: "IDCARD", cn: "身份证" },
    { key: "BANK", cn: "银行卡/账号" },
    { key: "EMAIL", cn: "邮箱" },
    { key: "ADDRESS", cn: "地址" },
    { key: "AMOUNT", cn: "报价/金额" },
    { key: "CODE", cn: "项目代号" },
    { key: "CREDITCODE", cn: "信用代码" }
  ];

  var STRATEGY_OPTIONS = [
    { value: "star", label: "星号遮盖（不可还原）" },
    { value: "fake", label: "假名替换（可还原）" },
    { value: "delete", label: "严格删除（不可还原）" }
  ];

  var PREVIEW_PAGE_SIZE = 30;
  var DOC_EXT = [".docx", ".txt", ".md", ".markdown"];
  var MAX_SIZE = 50 * 1024 * 1024;

  function $(id) { return document.getElementById(id); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function show(el) { if (el) el.classList.remove("hidden"); }
  function hide(el) { if (el) el.classList.add("hidden"); }

  function formatSize(bytes) {
    if (!bytes && bytes !== 0) return "";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1024 / 1024).toFixed(2) + " MB";
  }

  function extOf(name) {
    var idx = (name || "").lastIndexOf(".");
    return idx < 0 ? "" : name.slice(idx).toLowerCase();
  }

  var toastTimer = null;
  function toast(message, isError) {
    var el = $("toast");
    if (!el) return;
    el.textContent = message;
    el.classList.toggle("error", !!isError);
    show(el);
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { hide(el); }, 3200);
  }

  function setAlert(el, text, kind) {
    if (!el) return;
    el.classList.remove("error", "success", "warning");
    if (!text) { hide(el); el.textContent = ""; return; }
    el.classList.add(kind || "error");
    el.textContent = text;
    show(el);
  }

  function setLoading(btn, loading) {
    if (!btn) return;
    btn.classList.toggle("loading", !!loading);
    btn.disabled = !!loading;
  }

  function switchView(name) {
    qsa(".tab").forEach(function (tab) {
      tab.classList.toggle("active", tab.dataset.view === name);
    });
    qsa(".view").forEach(function (view) {
      view.classList.toggle("active", view.id === "view-" + name);
    });
    if (name === "exports") loadExports();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function setStep(step) {
    state.step = step;
    qsa(".steps .step-btn").forEach(function (btn, i) {
      btn.classList.toggle("active", i === step - 1);
      btn.disabled = i !== 0 && !state.jobId;
    });
  }

  function showStep(step) {
    if (!state.jobId && step > 1) step = 1;
    setStep(step);

    show($("uploadCard"));
    show($("reviewCard"));
    show($("previewCard"));

    if (step !== 1) hide($("uploadCard"));
    if (step !== 2) hide($("reviewCard"));
    if (step !== 3) hide($("previewCard"));

    // Step 2 底部导航固定
    var step2Nav = $("reviewCard .step-nav");
    if (step2Nav) step2Nav.classList.toggle("sticky", step === 2);
    var step3Nav = $("previewCard .step-nav");
    if (step3Nav) step3Nav.classList.toggle("sticky", step === 3);

    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // ---------------------------------------------------------------
  // 授权状态
  // ---------------------------------------------------------------
  var PLAN_NAMES = { pro: "专业版", team: "团队版", trial: "试用版" };

  function applyLicense(status) {
    if (!status) return;
    var machine = $("machineCode");
    if (machine) machine.value = status.machine_code || "";
    var badge = $("licenseBadge");
    var overlay = $("licenseOverlay");
    var server = $("licenseServer");
    if (server && status.server_url && !server.value) server.value = status.server_url;
    if (status.valid) {
      hide(overlay);
      document.body.classList.remove("locked");
      if (badge) {
        var text;
        if (status.dev) text = "开发模式";
        else if (status.in_grace) text = "宽限期剩余 " + status.grace_days_left + " 天";
        else text = (PLAN_NAMES[status.plan] || status.plan || "已授权") + " · 剩余 " + status.days_left + " 天";
        badge.className = "badge" + (status.valid && !status.in_grace ? " badge-green" : (status.in_grace ? " badge-warn" : ""));
        badge.textContent = text;
        badge.title = status.customer ? "授权给：" + status.customer : (status.message || "");
        show(badge);
      }
      return;
    }
    show(overlay);
    document.body.classList.add("locked");
    if (badge) {
      badge.className = "badge badge-warn";
      badge.textContent = "未激活";
      show(badge);
    }
    if (status.message) setAlert($("licenseMsg"), status.message, "error");
  }

  function checkLicense() {
    fetch("/api/license/status", { headers: { Accept: "application/json" } })
      .then(function (res) { return res.json(); })
      .then(applyLicense)
      .catch(function () {
        show($("licenseOverlay"));
        document.body.classList.add("locked");
        setAlert($("licenseMsg"), "无法连接本地服务，请重新启动软件", "error");
      });
  }

  function doActivate() {
    var code = ($("licenseCode").value || "").trim();
    var server = ($("licenseServer").value || "").trim();
    if (!code) { setAlert($("licenseMsg"), "请输入激活码", "error"); return; }
    var btn = $("activateBtn");
    setLoading(btn, true);
    setAlert($("licenseMsg"), "");
    fetch("/api/license/activate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: code, server_url: server })
    }).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) throw new Error(data.detail || "激活失败");
        return data;
      });
    }).then(function (status) {
      applyLicense(status);
      toast("激活成功，已绑定本机");
      checkHealth();
    }).catch(function (err) {
      setAlert($("licenseMsg"), err.message || "激活失败", "error");
    }).finally(function () { setLoading(btn, false); });
  }

  function copyMachineCode() {
    var input = $("machineCode");
    var value = (input && input.value) || "";
    if (!value) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).then(function () { toast("机器码已复制"); });
    } else {
      var readonly = input.hasAttribute("readonly");
      input.removeAttribute("readonly");
      input.select();
      try { document.execCommand("copy"); toast("机器码已复制"); } catch (e) { toast("复制失败", true); }
      if (readonly) input.setAttribute("readonly", "readonly");
    }
  }

  function bindLicense() {
    $("activateBtn").addEventListener("click", doActivate);
    $("copyMachine").addEventListener("click", copyMachineCode);
    $("licenseCode").addEventListener("keydown", function (e) { if (e.key === "Enter") doActivate(); });
  }

  function checkHealth() {
    fetch("/api/health", { headers: { Accept: "application/json" } })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        var badge = $("smartBadge");
        var smartOk = !!data.smart;
        var backendName = (data.ner && data.ner.active) === "onnx" ? "ONNX 模型"
          : ((data.ner && data.ner.active) === "jieba" ? "jieba 词性" : "");
        if (badge) {
          badge.textContent = smartOk
            ? "本地智能识别已就绪" + (backendName ? "（" + backendName + "）" : "")
            : "本地智能识别不可用";
        }
        var toggle = $("smartToggle");
        var hint = $("smartHint");
        if (toggle) {
          toggle.disabled = !smartOk;
          if (!smartOk) toggle.checked = false;
        }
        if (hint) hint.textContent = smartOk ? "补充词库外的人名/公司" : "未检测到 NER 后端，仅使用规则与词库";
      })
      .catch(function () {
        var badge = $("smartBadge");
        if (badge) badge.textContent = "服务未连接";
      });
  }

  // ---------------------------------------------------------------
  // 上传 + 识别
  // ---------------------------------------------------------------
  function selectFile(file) {
    setAlert($("uploadError"), "");
    if (!file) return;
    if (DOC_EXT.indexOf(extOf(file.name)) < 0) {
      setAlert($("uploadError"), "只支持 .docx / .txt / .md 文件，当前为：" + (extOf(file.name) || "未知格式"));
      return;
    }
    if (file.size > MAX_SIZE) { setAlert($("uploadError"), "文件超过 50MB 限制"); return; }
    if (file.size === 0) { setAlert($("uploadError"), "文件为空，请重新选择"); return; }
    state.file = file;
    var meta = $("fileMeta");
    meta.innerHTML = "";
    var name = document.createElement("span"); name.textContent = file.name;
    var size = document.createElement("span"); size.className = "size"; size.textContent = formatSize(file.size);
    meta.appendChild(name); meta.appendChild(size);
    show(meta);
    $("analyzeBtn").disabled = false;
  }

  function bindUpload() {
    var zone = $("dropzone");
    var input = $("fileInput");
    zone.addEventListener("click", function () { input.click(); });
    zone.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
    });
    input.addEventListener("change", function () { selectFile(input.files && input.files[0]); });
    ["dragenter", "dragover"].forEach(function (evt) {
      zone.addEventListener(evt, function (e) { e.preventDefault(); zone.classList.add("dragover"); });
    });
    ["dragleave", "drop"].forEach(function (evt) {
      zone.addEventListener(evt, function (e) { e.preventDefault(); zone.classList.remove("dragover"); });
    });
    zone.addEventListener("drop", function (e) {
      var files = e.dataTransfer && e.dataTransfer.files;
      if (files && files.length) { input.value = ""; selectFile(files[0]); }
    });
    window.addEventListener("dragover", function (e) { e.preventDefault(); });
    window.addEventListener("drop", function (e) { e.preventDefault(); });
  }

  function analyze() {
    if (!state.file || state.analyzing) return;
    state.analyzing = true;
    setAlert($("uploadError"), "");
    var btn = $("analyzeBtn");
    setLoading(btn, true);
    var form = new FormData();
    form.append("file", state.file, state.file.name);
    form.append("use_smart", $("smartToggle").checked ? "true" : "false");
    form.append("custom_words", $("customWords").value || "");

    fetch("/api/analyze", { method: "POST", body: form })
      .then(function (res) {
        return res.json().then(function (data) {
          if (!res.ok) throw new Error(data.detail || "识别失败");
          return data;
        });
      })
      .then(function (data) {
        state.analysis = data;
        state.jobId = data.job_id;
        renderReview(data);
        showStep(2);
        toast("识别完成：共 " + data.entities + " 个实体、" + data.occurrences + " 处出现");
      })
      .catch(function (err) {
        setAlert($("uploadError"), err.message || "识别失败，请重试");
      })
      .finally(function () { state.analyzing = false; setLoading(btn, false); });
  }

  // ---------------------------------------------------------------
  // 重新识别
  // ---------------------------------------------------------------
  function doReanalyze() {
    if (!state.jobId || state.analyzing) return;
    state.analyzing = true;
    var btn = $("addManualTermBtn");
    setLoading(btn, true);
    setAlert($("redactError"), "");

    fetch("/api/reanalyze", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: state.jobId,
        use_smart: $("smartToggle").checked,
        custom_words: $("customWords").value || ""
      })
    }).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) throw new Error(data.detail || "重新识别失败");
        return data;
      });
    }).then(function (data) {
      state.analysis = data;
      state.jobId = data.job_id;
      renderReview(data);
      toast("重新识别完成：共 " + data.entities + " 个实体、" + data.occurrences + " 处出现");
    }).catch(function (err) {
      setAlert($("redactError"), err.message || "重新识别失败，请重试");
    }).finally(function () { state.analyzing = false; setLoading(btn, false); });
  }

  function addManualTerm() {
    var sel = window.getSelection();
    var word = sel ? sel.toString().trim() : "";
    if (!word) { toast("请先在预览区域用鼠标选中要加入词库的原文片段", true); return; }
    if (word.length > 80) { toast("选中的词太长（上限 80 字符）", true); return; }
    var category = $("manualCategory").value;
    var categoryNames = {
      COMPANY: "公司", PERSON: "人名", PHONE: "电话", IDCARD: "身份证",
      BANK: "银行卡", EMAIL: "邮箱", ADDRESS: "地址", AMOUNT: "报价",
      CODE: "代号", CREDITCODE: "信用代码", WHITELIST: "白名单"
    };
    var spec = (categoryNames[category] || "公司") + "|" + word;
    var textarea = $("customWords");
    var lines = textarea.value.split("\n").map(function (l) { return l.trim(); }).filter(Boolean);
    if (lines.indexOf(spec) < 0) { lines.push(spec); textarea.value = lines.join("\n"); }
    sel.removeAllRanges();
    doReanalyze();
  }

  // ---------------------------------------------------------------
  // 预览
  // ---------------------------------------------------------------
  function collectOptions() {
    var strategies = {};
    qsa("#strategiesList select").forEach(function (select) {
      strategies[select.dataset.category] = select.value;
    });
    var excluded = [];
    qsa("#findingsList .finding").forEach(function (row) {
      var entityBox = row.querySelector(".finding-entity-check");
      var occurrenceBoxes = qsa(".finding-occurrence-check", row);
      if (entityBox && !entityBox.checked) { excluded.push(entityBox.dataset.key); return; }
      occurrenceBoxes.forEach(function (box) { if (!box.checked) excluded.push(box.dataset.key); });
    });
    var modeInput = document.querySelector("input[name=mode]:checked");
    return { mode: modeInput ? modeInput.value : "placeholder", strategies: strategies, excluded: excluded };
  }

  function schedulePreview() {
    if (!state.jobId) return;
    if (state.previewTimer) clearTimeout(state.previewTimer);
    state.previewTimer = setTimeout(loadPreview, 400);
  }

  function loadPreview() {
    if (!state.jobId) return;
    var seq = ++state.previewSeq;
    if (state.previewController) state.previewController.abort();
    state.previewController = new AbortController();
    var options = collectOptions();
    $("previewSubtitle").textContent = "正在生成对比预览…";

    fetch("/api/preview", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: state.jobId, mode: options.mode, strategies: options.strategies, excluded: options.excluded }),
      signal: state.previewController.signal
    }).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) throw new Error(data.detail || "预览生成失败");
        return data;
      });
    }).then(function (data) {
      if (seq !== state.previewSeq) return;
      state.preview = data;
      renderPreview(data);
    }).catch(function (err) {
      if (seq !== state.previewSeq) return;
      if (err && err.name === "AbortError") return;
      $("previewSubtitle").textContent = err.message || "预览生成失败";
      var body = $("compareBody"); body.innerHTML = "";
      var empty = document.createElement("div"); empty.className = "compare-empty";
      empty.textContent = "暂时无法生成预览，请稍后重试。";
      body.appendChild(empty);
    });
  }

  function renderPreview(data) {
    var all = (data && data.blocks) || [];
    var blocks = $("onlyChanged").checked ? all.filter(function (b) { return b.changed; }) : all;
    var totalPages = Math.max(1, Math.ceil(blocks.length / PREVIEW_PAGE_SIZE));
    if (state.previewPage >= totalPages) state.previewPage = totalPages - 1;
    if (state.previewPage < 0) state.previewPage = 0;
    var startIndex = state.previewPage * PREVIEW_PAGE_SIZE;
    var visible = blocks.slice(startIndex, startIndex + PREVIEW_PAGE_SIZE);

    var body = $("compareBody"); body.innerHTML = "";
    if (!blocks.length) {
      var empty = document.createElement("div"); empty.className = "compare-empty";
      empty.textContent = $("onlyChanged").checked
        ? "当前没有发生替换的内容块。"
        : "文档中没有可预览的文字内容。";
      body.appendChild(empty);
    } else {
      visible.forEach(function (block) {
        var row = document.createElement("div");
        row.className = "compare-row" + (block.changed ? " changed" : "");
        if (block.type === "table") {
          row.appendChild(buildTableSide(block, "original"));
          row.appendChild(buildTableSide(block, "redacted"));
        } else {
          row.appendChild(buildTextSide(block, "original"));
          row.appendChild(buildTextSide(block, "redacted"));
        }
        body.appendChild(row);
      });
    }

    $("previewSubtitle").textContent =
      ($("onlyChanged").checked ? "有变化内容块 " + blocks.length + " 个；" : "") +
      "全文共 " + data.blocks.length + " 个内容块，" + data.changed_blocks +
      " 个有变化；替换 " + data.entities + " 个实体、" + data.occurrences + " 处。";

    var pager = $("previewPager");
    if (pager) {
      pager.classList.toggle("hidden", blocks.length <= PREVIEW_PAGE_SIZE);
      $("previewPageInfo").textContent =
        "第 " + (state.previewPage + 1) + " / " + totalPages +
        " 页，当前显示 " + visible.length + " 个内容块";
      $("previewPrevBtn").disabled = state.previewPage === 0;
      $("previewNextBtn").disabled = state.previewPage >= totalPages - 1;
    }
  }

  function buildTextSide(block, side) {
    var cell = document.createElement("div");
    cell.className = "compare-cell doc-cell";
    var parts = block[side + "_parts"];
    var hlClass = side === "original" ? "hl-original" : "hl-redacted";
    if (block.label) {
      var label = document.createElement("span");
      label.className = "doc-label"; label.textContent = block.label;
      cell.appendChild(label);
    }
    var paragraph = document.createElement(block.type === "heading" ? "div" : "p");
    if (block.type === "heading") {
      var level = Math.min(Math.max(block.level || 1, 1), 6);
      paragraph.className = "doc-heading doc-h" + level;
    } else { paragraph.className = "doc-p"; }
    applyStyle(paragraph, block.style);
    fillParts(paragraph, parts, hlClass);
    cell.appendChild(paragraph);
    return cell;
  }

  function buildTableSide(block, side) {
    var cell = document.createElement("div");
    cell.className = "compare-cell doc-cell doc-cell-table";
    var hlClass = side === "original" ? "hl-original" : "hl-redacted";
    var table = document.createElement("table"); table.className = "doc-table";
    if (block.col_widths && block.col_widths.length) {
      var colgroup = document.createElement("colgroup");
      block.col_widths.forEach(function (width) {
        var col = document.createElement("col"); col.style.width = width;
        colgroup.appendChild(col);
      });
      table.appendChild(colgroup);
      table.style.tableLayout = "fixed";
    }
    (block.rows || []).forEach(function (row) {
      var tr = document.createElement("tr");
      row.forEach(function (cellData) {
        var td = document.createElement("td");
        applyStyle(td, cellData.style);
        fillParts(td, cellData[side + "_parts"], hlClass);
        tr.appendChild(td);
      });
      table.appendChild(tr);
    });
    cell.appendChild(table);
    return cell;
  }

  function fillParts(container, parts, hlClass) {
    (parts || []).forEach(function (part) {
      if (part.changed) {
        var mark = document.createElement("mark");
        mark.className = hlClass;
        mark.textContent = part.text;
        mark.dataset.cat = part.category_cn || "";
        mark.title = part.category_cn ? part.category_cn + "：" + (part.text || "已删除") : "";
        applyStyle(mark, part.style);
        container.appendChild(mark);
      } else {
        var text = part.text || "";
        var styleKeys = Object.keys(part.style || {});
        if (!styleKeys.length) { container.appendChild(document.createTextNode(text)); }
        else {
          var span = document.createElement("span");
          span.textContent = text;
          applyStyle(span, part.style);
          container.appendChild(span);
        }
      }
    });
  }

  function applyStyle(el, styles) {
    Object.keys(styles || {}).forEach(function (key) {
      var value = styles[key];
      if (value !== null && value !== undefined && value !== "") el.style[key] = value;
    });
  }

  // ---------------------------------------------------------------
  // 复核渲染
  // ---------------------------------------------------------------
  function renderReview(data) {
    $("reviewSubtitle").textContent =
      data.file.name + " · " + formatSize(data.file.size) + " · 共 " +
      data.entities + " 个实体、" + data.occurrences + " 处出现。请取消勾选误标项，右侧可实时预览脱敏效果。";

    var chips = $("summaryChips"); chips.innerHTML = "";
    data.categories.forEach(function (cat) {
      var chip = document.createElement("span");
      chip.className = "chip cat-" + cat.key;
      chip.innerHTML = "<span>" + cat.cn + "</span><b>" + cat.entities + "</b><span>个 · " + cat.occurrences + " 处</span>";
      chips.appendChild(chip);
    });

    renderFindings();
    if (!$("strategiesList").children.length) renderStrategies(ALL_CATEGORIES);
    updateSelectionInfo();
    setAlert($("redactError"), "");
    $("searchBox").value = "";
    $("selectAll").checked = true;
    $("onlyChanged").checked = false;
    state.previewPage = 0;
    updateReviewHint();
    state.result = null;
    showStep(2);
    // Step 2 也要加载预览（虽然 Step 2 模板里暂时没有 compare 区，但 Step 3 有）
    loadPreview();
  }

  function renderFindings() {
    var list = $("findingsList"); list.innerHTML = "";
    var findings = (state.analysis && state.analysis.findings) || [];
    if (!findings.length) {
      var empty = document.createElement("div"); empty.className = "empty";
      empty.textContent = "未识别到敏感信息。可在「智能识别与补充词」中按 类别|词 手动添加后重新识别。";
      list.appendChild(empty); return;
    }
    findings.forEach(function (finding) {
      var row = document.createElement("div");
      row.className = "finding";
      row.dataset.key = finding.key;
      var contexts = (finding.contexts || []).concat((finding.occurrences || []).map(function (o) { return o.context; })).join(" ");
      row.dataset.search = (finding.text + " " + finding.category_cn + " " + finding.category + " " + contexts)
        .replace(/[⟦⟧]/g, "").toLowerCase();

      var box = document.createElement("input");
      box.type = "checkbox"; box.checked = true; box.className = "finding-entity-check"; box.dataset.key = finding.key;

      var badge = document.createElement("span");
      badge.className = "cat-badge cat-" + finding.category;
      badge.textContent = finding.category_cn;

      var main = document.createElement("span"); main.className = "finding-main";
      var text = document.createElement("span"); text.className = "finding-text"; text.textContent = finding.text;
      main.appendChild(text);
      var count = document.createElement("span"); count.className = "count"; count.textContent = finding.count + " 处";

      row.appendChild(box); row.appendChild(badge); row.appendChild(main); row.appendChild(count);
      var occurrences = finding.occurrences || [];
      if (!occurrences.length && finding.contexts && finding.contexts.length) {
        occurrences = finding.contexts.map(function (context, index) { return { id: finding.key + ":" + index, context: context }; });
      }
      var occurrenceWrap = document.createElement("div"); occurrenceWrap.className = "occurrence-list";
      occurrences.forEach(function (occurrence, index) {
        var occurrenceLabel = document.createElement("label"); occurrenceLabel.className = "occurrence-item";
        var occurrenceBox = document.createElement("input");
        occurrenceBox.type = "checkbox"; occurrenceBox.checked = true;
        occurrenceBox.className = "finding-occurrence-check"; occurrenceBox.dataset.key = occurrence.id;
        var occurrenceText = document.createElement("span"); occurrenceText.className = "occurrence-text";
        occurrenceText.textContent = "第 " + (index + 1) + " 处：" + occurrence.context.replace(/[⟦⟧]/g, "");
        occurrenceLabel.appendChild(occurrenceBox); occurrenceLabel.appendChild(occurrenceText);
        occurrenceWrap.appendChild(occurrenceLabel);
      });
      row.appendChild(occurrenceWrap); list.appendChild(row);
    });
  }

  function renderStrategies(categories) {
    var wrap = $("strategiesList"); wrap.innerHTML = "";
    var cats = (categories || []).slice().sort(function (a, b) { return CATEGORY_ORDER.indexOf(a.key) - CATEGORY_ORDER.indexOf(b.key); });
    cats.forEach(function (cat) {
      var row = document.createElement("div"); row.className = "strategy-row";
      var label = document.createElement("label");
      label.textContent = cat.cn; label.setAttribute("for", "strategy-" + cat.key);
      var select = document.createElement("select");
      select.id = "strategy-" + cat.key; select.dataset.category = cat.key;
      STRATEGY_OPTIONS.forEach(function (opt) {
        var option = document.createElement("option");
        option.value = opt.value; option.textContent = opt.label;
        select.appendChild(option);
      });
      select.value = "star";
      select.addEventListener("change", function () { updateReviewHint(); schedulePreview(); });
      row.appendChild(label); row.appendChild(select); wrap.appendChild(row);
    });
  }

  function updateSelectionInfo() {
    var entityBoxes = qsa("#findingsList .finding-entity-check");
    var occurrenceBoxes = qsa("#findingsList .finding-occurrence-check");
    var selectedOccurrences = 0;
    qsa("#findingsList .finding").forEach(function (row) {
      var entityBox = row.querySelector(".finding-entity-check");
      if (entityBox && !entityBox.checked) return;
      var rowOccurrences = qsa(".finding-occurrence-check", row);
      selectedOccurrences += rowOccurrences.length
        ? rowOccurrences.filter(function (b) { return b.checked; }).length : 1;
    });
    $("selectionInfo").textContent = "已选 " + selectedOccurrences + " 处（" + entityBoxes.length + " 个实体）";
    $("selectAll").checked = entityBoxes.length > 0 &&
      entityBoxes.every(function (b) { return b.checked; }) &&
      occurrenceBoxes.every(function (b) { return b.checked; });
  }

  function findFinding(key) {
    var findings = (state.analysis && state.analysis.findings) || [];
    for (var i = 0; i < findings.length; i++) { if (findings[i].key === key) return findings[i]; }
    return null;
  }

  function updateReviewHint() {
    var hint = $("reviewHint");
    if (!hint) return;
    var selectedOccurrences = [];
    qsa("#findingsList .finding").forEach(function (row) {
      var entityBox = row.querySelector(".finding-entity-check");
      if (entityBox && !entityBox.checked) return;
      var rowOccurrences = qsa(".finding-occurrence-check", row);
      if (rowOccurrences.length) {
        rowOccurrences.forEach(function (box) {
          if (box.checked) {
            var f = findFinding(row.dataset.key);
            if (f) selectedOccurrences.push(f.category);
          }
        });
      } else {
        var f = findFinding(row.dataset.key);
        if (f) selectedOccurrences.push(f.category);
      }
    });
    if (!selectedOccurrences.length) {
      hint.textContent = "未选择任何实体，无法生成脱敏文档";
      return;
    }
    var irreversible = 0;
    qsa("#strategiesList select").forEach(function (select) {
      if (select.value !== "fake") {
        var catCount = selectedOccurrences.filter(function (cat) { return cat === select.dataset.category; }).length;
        if (catCount) irreversible += catCount;
      }
    });
    hint.textContent = "将处理 " + selectedOccurrences.length + " 处出现" +
      (irreversible ? "（" + irreversible + " 处不可还原）" : "，全部可还原");
  }

  // ---------------------------------------------------------------
  // 脱敏 + 导出
  // ---------------------------------------------------------------
  function openExportModal() {
    if (!state.jobId) return;
    if (!state.file) return;
    var filename = state.file.name.replace(/\.[^.]+$/, "") + "_脱敏.docx";
    $("exportFilename").textContent = filename;
    $("modalPassword").value = "";
    $("modalPassword").type = "password";
    $("modalTogglePwd").textContent = "显示";
    $("modalWarning").classList.add("hidden");
    show($("exportModal"));
    $("modalPassword").focus();
  }

  function closeExportModal() { hide($("exportModal")); }

  function updateModalWarning() {
    var pwd = $("modalPassword").value;
    $("modalWarning").classList.toggle("hidden", !!pwd);
  }

  function doRedact() {
    if (!state.jobId || state.redacting) return;
    state.redacting = true;
    closeExportModal();
    setAlert($("redactError"), "");
    var btn = $("exportBtn");
    setLoading(btn, true);

    var options = collectOptions();
    var payload = {
      job_id: state.jobId, mode: options.mode,
      strategies: options.strategies, excluded: options.excluded,
      password: $("modalPassword").value || ""
    };

    fetch("/api/redact", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) throw new Error(data.detail || "脱敏失败");
        return data;
      });
    }).then(function (data) {
      state.result = data;
      toast("脱敏完成：共 " + data.occurrences + " 处替换");
      showResultModal(data);
    }).catch(function (err) {
      toast("脱敏失败：" + (err.message || "未知错误"), true);
    }).finally(function () { state.redacting = false; setLoading(btn, false); });
  }

  function showResultModal(data) {
    var subtitle = "共 " + data.entities + " 个实体、" + data.occurrences + " 处替换";
    if (data.irreversible_entities) subtitle += "；其中 " + data.irreversible_entities + " 个实体使用不可还原策略";
    subtitle += "。耗时 " + data.elapsed + " 秒。";
    $("resultSubtitle").textContent = subtitle;

    var stats = $("resultStats"); stats.innerHTML = "";
    Object.keys(data.stats || {}).forEach(function (key) {
      var chip = document.createElement("span"); chip.className = "chip";
      chip.innerHTML = "<span>" + key + "</span><b>" + data.stats[key] + "</b><span>处</span>";
      stats.appendChild(chip);
    });

    var downloads = $("resultDownloads"); downloads.innerHTML = "";
    (data.files || []).forEach(function (file) {
      var card = document.createElement("button");
      card.type = "button"; card.className = "download-card";
      var icon = document.createElement("span"); icon.className = "dl-icon";
      icon.textContent = file.kind === "redacted" ? "DOC"
        : (file.kind === "mapping" ? "MAP" : "LOG");
      var main = document.createElement("span"); main.className = "dl-main";
      var title = document.createElement("b");
      title.textContent = file.kind === "redacted" ? "脱敏后的文档"
        : (file.kind === "mapping" ? "映射文件（还原必需）" : "处理报告");
      var small = document.createElement("small");
      small.textContent = file.name + " · " + formatSize(file.size);
      main.appendChild(title); main.appendChild(small);
      card.appendChild(icon); card.appendChild(main);
      card.addEventListener("click", function () { downloadFile(file); });
      downloads.appendChild(card);
    });

    $("nextBtn").onclick = function () { hide($("resultModal")); resetWork(); };
    show($("resultModal"));
  }

  function downloadFile(file) {
    if (!file || !file.url) { toast("导出文件未就绪", true); return; }
    // pywebview 桌面壳优先
    if (window.pywebview && window.pywebview.api && typeof window.pywebview.api.save_file === "function") {
      toast("准备保存文件…");
      fetch(file.url).then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.arrayBuffer();
      }).then(function (buf) {
        var bytes = new Uint8Array(buf);
        var binary = "";
        for (var i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
        var b64 = btoa(binary);
        return window.pywebview.api.save_file(b64, file.name);
      }).then(function (savedPath) {
        if (savedPath) toast("已保存：" + savedPath);
        else toast("已取消保存", true);
      }).catch(function (err) {
        toast("保存失败：" + (err && err.message ? err.message : String(err)), true);
      });
      return;
    }
    toast("正在下载：" + file.name);
    fetch(file.url).then(function (res) {
      if (!res.ok) throw new Error("HTTP " + res.status);
      return res.blob();
    }).then(function (blob) {
      var url = URL.createObjectURL(blob);
      var a = document.createElement("a");
      a.href = url; a.download = file.name;
      document.body.appendChild(a); a.click();
      document.body.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      toast("已保存：" + file.name + "（" + formatSize(file.size) + "）");
    }).catch(function (err) {
      toast("下载失败：" + err.message, true);
    });
  }

  function resetWork() {
    state.file = null; state.analysis = null; state.jobId = null;
    $("fileInput").value = ""; hide($("fileMeta")); $("fileMeta").innerHTML = "";
    $("analyzeBtn").disabled = true;
    state.preview = null; state.previewPage = 0; state.previewSeq += 1;
    if (state.previewTimer) clearTimeout(state.previewTimer);
    state.result = null;
    showStep(1);
    setAlert($("uploadError"), ""); setAlert($("redactError"), "");
  }

  // ---------------------------------------------------------------
  // 还原
  // ---------------------------------------------------------------
  function bindRestore() {
    $("restoreDoc").addEventListener("change", function () {
      var f = this.files && this.files[0];
      $("restoreDocName").textContent = f ? f.name + " · " + formatSize(f.size) : "选择 .docx / .txt / .md";
    });
    $("restoreMap").addEventListener("change", function () {
      var f = this.files && this.files[0];
      $("restoreMapName").textContent = f ? f.name + " · " + formatSize(f.size) : "选择 .enc / .json";
    });
    $("restoreBtn").addEventListener("click", doRestore);
  }

  function doRestore() {
    if (state.restoring) return;
    var doc = $("restoreDoc").files && $("restoreDoc").files[0];
    var map = $("restoreMap").files && $("restoreMap").files[0];
    var msg = $("restoreMsg"); msg.innerHTML = ""; hide($("restoreResult"));
    if (!doc || !map) { setAlert(msg, "请同时选择脱敏后的文档和映射文件", "error"); return; }
    if (DOC_EXT.indexOf(extOf(doc.name)) < 0) { setAlert(msg, "文档格式不支持", "error"); return; }
    if ([".enc", ".json", ".maskmap"].indexOf(extOf(map.name)) < 0) { setAlert(msg, "映射文件格式不支持", "error"); return; }

    state.restoring = true;
    var btn = $("restoreBtn"); setLoading(btn, true);
    var form = new FormData();
    form.append("redacted", doc, doc.name);
    form.append("mapping", map, map.name);
    form.append("password", $("restorePwd").value || "");

    fetch("/api/restore", { method: "POST", body: form }).then(function (res) {
      return res.json().then(function (data) { if (!res.ok) throw new Error(data.detail || "还原失败"); return data; });
    }).then(function (data) {
      setAlert(msg, "还原完成：共替换 " + data.replaced + " 处敏感信息。", "success");
      var grid = $("restoreResult"); grid.innerHTML = "";
      var card = document.createElement("button");
      card.type = "button"; card.className = "download-card";
      var icon = document.createElement("span"); icon.className = "dl-icon"; icon.textContent = "DOC";
      var main = document.createElement("span"); main.className = "dl-main";
      var title = document.createElement("b"); title.textContent = "还原后的文档";
      var small = document.createElement("small"); small.textContent = data.file.name + " · " + formatSize(data.file.size);
      main.appendChild(title); main.appendChild(small);
      card.appendChild(icon); card.appendChild(main);
      card.addEventListener("click", function () { downloadFile(data.file); });
      grid.appendChild(card); show(grid);
      toast("还原完成");
    }).catch(function (err) {
      setAlert(msg, err.message || "还原失败，请检查口令与文件是否匹配", "error");
    }).finally(function () { state.restoring = false; setLoading(btn, false); });
  }

  // ---------------------------------------------------------------
  // 导出历史
  // ---------------------------------------------------------------
  function loadExports() {
    var list = $("exportsList"); list.innerHTML = "";
    fetch("/api/exports").then(function (res) {
      return res.json().then(function (d) { if (!res.ok) throw new Error(d.detail || "加载失败"); return d; });
    }).then(function (data) {
      var exports = data.exports || [];
      if (!exports.length) {
        var empty = document.createElement("div"); empty.className = "empty";
        empty.textContent = "暂无导出记录。完成脱敏导出后会自动在此显示。";
        list.appendChild(empty); return;
      }
      exports.forEach(function (item) {
        var card = document.createElement("div"); card.className = "export-item";
        var head = document.createElement("div"); head.className = "export-item-head";
        head.innerHTML = "<b>" + item.filename + "</b>" +
          "<span class='hint'>导出于 " + item.exported_at + " · 共 " + item.entities + " 个实体 / " +
          item.occurrences + " 处替换" + (item.mapping_encrypted ? " · 映射已加密" : "") + "</span>";
        card.appendChild(head);
        var fileRow = document.createElement("div"); fileRow.className = "export-files";
        (item.files || []).forEach(function (f) {
          var label = f.kind === "redacted" ? "脱敏文档" : (f.kind === "mapping" ? "映射文件" : "处理报告");
          var btnWrap = document.createElement("div"); btnWrap.className = "export-file-row";
          var info = document.createElement("span");
          info.innerHTML = "<b>" + label + "</b><small>" + f.name + (f.size ? " · " + formatSize(f.size) : "") + "</small>";
          btnWrap.appendChild(info);
          if (f.exists && f.url) {
            var dl = document.createElement("button"); dl.className = "btn ghost small"; dl.textContent = "下载";
            dl.addEventListener("click", function () { downloadFile({ name: f.name, size: f.size, url: f.url }); });
            btnWrap.appendChild(dl);
          } else {
            var gone = document.createElement("span"); gone.className = "hint"; gone.textContent = "源文件已过期";
            btnWrap.appendChild(gone);
          }
          fileRow.appendChild(btnWrap);
        });
        card.appendChild(fileRow);
        list.appendChild(card);
      });
    }).catch(function (err) {
      var empty = document.createElement("div"); empty.className = "empty";
      empty.textContent = "加载导出历史失败：" + err.message;
      list.appendChild(empty);
    });
  }

  // ---------------------------------------------------------------
  // 事件绑定
  // ---------------------------------------------------------------
  function bindReview() {
    $("searchBox").addEventListener("input", function () {
      var q = this.value.trim().toLowerCase();
      qsa("#findingsList .finding").forEach(function (row) {
        row.style.display = !q || row.dataset.search.indexOf(q) >= 0 ? "" : "none";
      });
    });
    $("selectAll").addEventListener("change", function () {
      var checked = this.checked;
      qsa("#findingsList input[type=checkbox]").forEach(function (box) {
        box.checked = checked;
        box.closest(".finding").classList.toggle("excluded", !checked);
      });
      updateSelectionInfo(); updateReviewHint(); schedulePreview();
    });
    // 在 compareBody 上监听鼠标事件以便用户选中原文后补词
    $("compareBody").addEventListener("mouseup", function () {
      // 选中文本后自动开启手动补词入口
    });
    $("findingsList").addEventListener("change", function (e) {
      var target = e.target;
      if (!target || target.type !== "checkbox") return;
      if (target.classList.contains("finding-occurrence-check")) {
        target.closest(".occurrence-item").classList.toggle("excluded", !target.checked);
        var row = target.closest(".finding");
        var entityBox = row.querySelector(".finding-entity-check");
        if (target.checked && entityBox) entityBox.checked = true;
      } else {
        target.closest(".finding").classList.toggle("excluded", !target.checked);
        var occBoxes = qsa(".finding-occurrence-check", target.closest(".finding"));
        occBoxes.forEach(function (box) {
          box.checked = target.checked;
          box.closest(".occurrence-item").classList.toggle("excluded", !target.checked);
        });
      }
      updateSelectionInfo(); updateReviewHint(); schedulePreview();
    });
    $("onlyChanged").addEventListener("change", function () { state.previewPage = 0; if (state.preview) renderPreview(state.preview); });
    qsa("input[name=mode]").forEach(function (radio) { radio.addEventListener("change", schedulePreview); });

    $("rechooseBtn").addEventListener("click", resetWork);
    $("addManualTermBtn").addEventListener("click", addManualTerm);
    $("previewPrevBtn").addEventListener("click", function () {
      if (state.previewPage > 0) { state.previewPage -= 1; if (state.preview) renderPreview(state.preview); }
    });
    $("previewNextBtn").addEventListener("click", function () {
      state.previewPage += 1; if (state.preview) renderPreview(state.preview);
    });

    // Step 1 → Step 2
    $("backUploadBtn").addEventListener("click", function () { showStep(1); });
    $("goPreviewBtn").addEventListener("click", function () { showStep(3); schedulePreview(); });

    // Step 3 → Step 2
    $("backReviewBtn").addEventListener("click", function () { showStep(2); schedulePreview(); });
    $("backReviewBtn2").addEventListener("click", function () { showStep(2); schedulePreview(); });

    // Step 切换
    $("stepBtn1").addEventListener("click", function () { showStep(1); });
    $("stepBtn2").addEventListener("click", function () { showStep(2); schedulePreview(); });
    $("stepBtn3").addEventListener("click", function () { showStep(3); schedulePreview(); });

    // 导出按钮
    $("exportBtn").addEventListener("click", openExportModal);
    $("modalCancel").addEventListener("click", closeExportModal);
    $("modalConfirm").addEventListener("click", doRedact);
    $("modalTogglePwd").addEventListener("click", function () {
      var input = $("modalPassword");
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      this.textContent = show ? "隐藏" : "显示";
    });
    $("modalPassword").addEventListener("input", updateModalWarning);

    // 结果弹窗
    $("resultClose").addEventListener("click", function () { hide($("resultModal")); });

    // 导出历史
    $("refreshExports").addEventListener("click", loadExports);
  }

  function bindSteps() {
    // 只有识别完成后才允许切换 step 2/3
  }

  // ---------------------------------------------------------------
  // 初始化
  // ---------------------------------------------------------------
  function init() {
    qsa(".tab").forEach(function (tab) {
      tab.addEventListener("click", function () { switchView(tab.dataset.view); });
    });
    bindUpload(); bindReview(); bindRestore(); bindLicense();
    renderStrategies(ALL_CATEGORIES);
    $("analyzeBtn").addEventListener("click", analyze);
    $("smartToggle").addEventListener("change", function () {
      $("smartHint").textContent = this.checked ? "补充词库外的人名/公司" : "仅使用规则与词库";
    });
    checkHealth(); checkLicense();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
