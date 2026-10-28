/* 后台管理表格：多选 + 二次确认 + 无刷新删除。
   ------------------------------------------------------------------
   渐进增强：无 JS / 无 fetch 时，表单照常提交并走服务端 PRG 重定向，
   行为与改造前完全一致；本文件只让「有 JS 的浏览器」少一次整页刷新。

   二次确认不自己弹窗，而是复用 base.html 里已有的 data-confirm 机制
   （Bootstrap 模态框，无 Bootstrap 时退回 window.confirm），
   保证全站危险操作的确认观感一致。
   ------------------------------------------------------------------ */
(function () {
    "use strict";
    var App = window.App || (window.App = {});

    function qsa(root, selector) {
        return Array.prototype.slice.call((root || document).querySelectorAll(selector));
    }

    function toast(message, type) {
        if (typeof App.toast === "function") App.toast(message, type || "ok");
    }

    /* ---------- 一、多选：勾选 → 计数 → 批量按钮可用 ---------- */
    Array.prototype.forEach.call(
        document.querySelectorAll("[data-admin-scope]"),
        function (scope) {
            setupScope(scope);
        }
    );

    /* 行首勾选框（关联管理的「整行」）跟随行内各条关联的三态 */
    function syncRowBoxes(scope) {
        qsa(scope, "input[type='checkbox'][data-select-row]").forEach(function (rowBox) {
            var row = rowBox.closest("tr");
            if (!row) return;
            var chips = qsa(row, "input[type='checkbox'][data-chip-check]");
            var picked = chips.filter(function (chip) { return chip.checked; }).length;
            rowBox.checked = chips.length > 0 && picked === chips.length;
            rowBox.indeterminate = picked > 0 && picked < chips.length;
        });
    }

    function setupScope(scope) {
        /* 一个面板可以挂多个批量表单（作品审核有「通过 / 驳回」两个动作），
           每个按钮通过自己的 form 属性找到归属表单。 */
        var bulkBtns = qsa(scope, "[data-bulk-submit]").filter(function (btn) {
            return !!document.getElementById(btn.getAttribute("form") || "");
        });
        if (!bulkBtns.length) return;
        var countEl = scope.querySelector("[data-sel-count]");
        /* 全选控件可能有两个：桌面在表头、窄屏在批量栏（表头整行在手机上被隐藏）。
           两者都挂 data-select-all，状态由 refresh 统一同步。 */
        var selectAlls = qsa(scope, "[data-select-all]");

        function boxes() {
            return qsa(
                scope,
                "input[type='checkbox'][data-row-check]," +
                "input[type='checkbox'][data-chip-check]"
            );
        }

        /* 勾选状态 → 计数文案 + 各批量按钮的可用性与数量标签。
           0 选中时禁用按钮：比「点下去再报错」更早给出反馈。 */
        function refresh() {
            var all = boxes();
            var picked = all.filter(function (box) { return box.checked; }).length;
            if (countEl) countEl.textContent = String(picked);
            bulkBtns.forEach(function (btn) {
                var label = btn.getAttribute("data-bulk-label") || btn.textContent.trim();
                btn.setAttribute("data-bulk-label", label);
                btn.disabled = picked === 0;
                btn.textContent = picked > 0 ? label + "（" + picked + "）" : label;
            });
            selectAlls.forEach(function (el) {
                el.checked = all.length > 0 && picked === all.length;
                el.indeterminate = picked > 0 && picked < all.length;
            });
            syncRowBoxes(scope);
        }

        scope.addEventListener("change", function (event) {
            var box = event.target;
            if (!box || box.type !== "checkbox") return;
            if (box.hasAttribute("data-select-all")) {
                boxes().forEach(function (item) { item.checked = box.checked; });
                syncRowBoxes(scope);
            } else if (box.hasAttribute("data-select-row")) {
                /* 关联管理：一个作品有若干条关联，行首勾选框一次选整行 */
                var row = box.closest("tr");
                if (row) {
                    qsa(row, "input[type='checkbox'][data-chip-check]").forEach(function (item) {
                        item.checked = box.checked;
                    });
                }
            } else if (!box.hasAttribute("data-row-check")
                && !box.hasAttribute("data-chip-check")) {
                return;
            }
            refresh();
        });

        /* 点批量按钮时才生成确认文案（选中数此刻才确定），
           随后交给 base.html 的确认弹窗拦截，不自己弹窗。 */
        bulkBtns.forEach(function (btn) {
            btn.addEventListener("click", function (event) {
                var form = document.getElementById(btn.getAttribute("form") || "");
                if (!form) return;
                var picked = boxes().filter(function (box) { return box.checked; }).length;
                if (picked === 0) {
                    event.preventDefault();
                    toast("请先勾选要处理的条目。", "err");
                    return;
                }
                var verb = form.getAttribute("data-verb") || "处理";
                var unit = form.getAttribute("data-unit") || "条";
                var entity = form.getAttribute("data-entity") || "条目";
                /* 默认警示「不可撤销」；通过 / 驳回这类可再改的操作在模板上
                   显式写 data-confirm-tail="" 才去掉这句。 */
                var tail = form.getAttribute("data-confirm-tail");
                if (tail === null) tail = "此操作不可撤销。";
                form.setAttribute(
                    "data-confirm",
                    "确定" + verb + "选中的 " + picked + " " + unit + entity + "？" + tail
                );
                form.setAttribute("data-confirm-title", "确认" + verb);
                form.setAttribute("data-confirm-ok", "确认" + verb);
            });
        });

        scope.__refreshSelection = refresh;
        refresh();
    }

    /* ---------- 二、无刷新删除 / 作废 ---------- */

    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!(form instanceof HTMLFormElement)) return;
        /* defaultPrevented：base.html 的确认弹窗正在拦截（首次提交），此时不插手 */
        if (event.defaultPrevented) return;
        if (!form.hasAttribute("data-ajax-delete") && !form.hasAttribute("data-ajax-revoke")) return;
        if (typeof window.fetch !== "function" || !window.FormData) return;
        event.preventDefault();
        runAjax(form, event.submitter);
    }, true);

    /* 本次提交的 id 列表。
       单条表单直接写在 data-ids 上；批量表单没有自己的勾选框（勾选框在表格里，
       靠 form 属性挂过来），取作用域内处于勾选状态的那些。
       注意：不能用 FormData(form) 一把梭——那样会把 form 属性挂来的勾选框
       与手工补的 id 一起收进来，同一个 id 出现两次，批量处理会被算成两次结果。 */
    function formIds(form) {
        var attr = form.getAttribute("data-ids");
        if (attr) {
            return attr.split(",")
                .map(function (text) { return text.trim(); })
                .filter(function (text) { return text !== ""; });
        }
        var fields = (form.getAttribute("data-id-field") || "ids").split(",");
        var scope = form.closest("[data-admin-scope]");
        var picked = [];
        var collect = function (input) {
            if (input.type === "checkbox" && !input.checked) return;
            if (fields.indexOf(input.name) === -1) return;
            if (picked.indexOf(input.value) === -1) picked.push(input.value);
        };
        if (scope) {
            qsa(scope, "input[type='checkbox']").forEach(collect);
        }
        if (picked.length) return picked;
        qsa(form, "input").forEach(collect);
        return picked;
    }

    /* 请求体：表单自身的隐藏字段（筛选条件）+ 上面算出的 id 列表。
       显式拼装而不是 FormData(form)，id 才不会重复。 */
    function buildBody(form, ids) {
        var idField = form.getAttribute("data-id-field") || "ids";
        var data = new FormData();
        qsa(form, "input").forEach(function (input) {
            if (!input.name || input.type === "checkbox") return;
            if (input.name === idField) return; /* id 单独加，避免重复 */
            data.append(input.name, input.value);
        });
        ids.forEach(function (id) { data.append(idField, id); });
        return new URLSearchParams(data);
    }

    /* 服务端把结果写在重定向 URL 上（deleted=N / revoked=N），这里读回来 */
    function countFromUrl(url, param) {
        var index = url.indexOf("?");
        if (index === -1) return 0;
        var match = url.slice(index + 1).match(
            new RegExp("(?:^|&)" + param + "=(\\d*)")
        );
        return match ? (parseInt(match[1], 10) || 0) : 0;
    }

    function removeTarget(id, mode) {
        if (mode === "chip") {
            qsa(document, "[data-chip-id='" + id + "']").forEach(function (chip) {
                var cell = chip.closest("td");
                chip.remove();
                /* 一部作品的关联被删光后，别留一个空的「作者/公司关联」行 */
                if (cell && !cell.querySelector("[data-chip-id]")) {
                    cell.innerHTML = '<span class="text-muted small">未识别到</span>';
                }
            });
            return;
        }
        var row = document.querySelector("[data-row-id='" + id + "']");
        if (!row) return;
        if (mode === "mark") {
            /* 作废不是删除：行还在，只是状态改了、不能再操作 */
            var status = row.querySelector("[data-status-cell]");
            if (status) status.innerHTML = '<span class="badge bg-danger">已作废</span>';
            var actions = row.querySelector(".cell-actions");
            if (actions) actions.innerHTML = '<span class="text-muted small">—</span>';
            var box = row.querySelector("input[type='checkbox']");
            if (box) {
                box.checked = false;
                box.disabled = true;
            }
            return;
        }
        row.remove();
    }

    function adjustTotal(removed) {
        var el = document.querySelector("[data-admin-total]");
        if (!el) return;
        var current = parseInt(el.textContent, 10);
        if (isNaN(current)) return;
        el.textContent = String(Math.max(0, current - removed));
    }

    /* 删掉条目后总页数会变：把「第 X / Y 页」与跳页上限同步过来，
       否则跳页框还能跳到已经不存在的页。只有一页时 macro 不渲染分页，直接跳过。 */
    function updatePager(scope, total) {
        var size = parseInt(scope.getAttribute("data-page-size") || "0", 10);
        if (!size) return;
        var nav = scope.querySelector(".pagination");
        if (!nav) return;
        var label = null;
        qsa(nav, "span.page-link").forEach(function (el) {
            if (el.textContent.indexOf("第") !== -1) label = el;
        });
        if (!label) return;
        var parsed = label.textContent.match(/第\s*(\d+)\s*\/\s*(\d+)\s*页/);
        if (!parsed) return;
        var page = parseInt(parsed[1], 10);
        var pages = parseInt(parsed[2], 10);
        var newPages = isNaN(total) ? pages : Math.max(1, Math.ceil(total / size));
        if (newPages === pages) return;
        label.textContent = "第 " + page + " / " + newPages + " 页";
        var jump = scope.querySelector(".pager-jump-input");
        if (jump) {
            jump.max = String(newPages);
            if (parseInt(jump.value, 10) > newPages) jump.value = String(newPages);
        }
        var jumpBtn = scope.querySelector(".pager-jump-btn");
        if (jumpBtn) jumpBtn.setAttribute("data-pager-max", String(newPages));
    }

    /* 解锁：base.html 的防重复提交会禁用按钮并插入 spinner，
       AJAX 自己接管了加载态，这里统一清干净（失败回退提交前也必须清）。 */
    function unlock(form, submitter) {
        qsa(form, "button").forEach(function (btn) {
            btn.disabled = false;
            var spinner = btn.querySelector(".btn-spinner");
            if (spinner) spinner.remove();
        });
        if (form.id) {
            qsa(document, "button[form='" + form.id + "']").forEach(function (btn) {
                btn.disabled = false;
            });
        }
        if (submitter) submitter.disabled = false;
    }

    function runAjax(form, submitter) {
        var verb = form.getAttribute("data-verb") || "删除";
        var unit = form.getAttribute("data-unit") || "条";
        var entity = form.getAttribute("data-entity") || "条目";
        var countParam = form.getAttribute("data-count-param") || "deleted";
        var mode = form.getAttribute("data-remove") || "row";
        var ids = formIds(form);

        if (submitter) submitter.disabled = true;

        fetch(form.action, {
            method: "POST",
            body: buildBody(form, ids),
            credentials: "same-origin",
            redirect: "follow",
        }).then(function (resp) {
            if (!resp.ok) throw new Error("http " + resp.status);
            return { removed: countFromUrl(resp.url || "", countParam) };
        }).then(function (result) {
            unlock(form, submitter);
            var removed = result.removed;
            if (removed > 0) {
                ids.forEach(function (id) { removeTarget(id, mode); });
                var scope = form.closest("[data-admin-scope]");
                adjustTotal(removed);
                if (scope) {
                    if (typeof scope.__refreshSelection === "function") {
                        scope.__refreshSelection();
                    }
                    var totalEl = document.querySelector("[data-admin-total]");
                    updatePager(
                        scope,
                        totalEl ? parseInt(totalEl.textContent, 10) : NaN
                    );
                    /* 本页删空了才整页刷新：分页与空态只有服务端算得准，
                       这是唯一一次「强制刷新」，且只在真的删空时发生。 */
                    if (scope.querySelector("tbody tr[data-row-id]") === null
                        && mode === "row") {
                        location.reload();
                        return;
                    }
                }
                toast("已" + verb + " " + removed + " " + unit + entity + "。", "ok");
            } else {
                toast("没有可" + verb + "的" + entity + "。", "err");
            }
        }).catch(function () {
            /* 网络异常 / 非 2xx（例如单条删除打到 404 页）：
               交回浏览器原生提交，操作绝不丢。 */
            unlock(form, submitter);
            if (typeof form.requestSubmit === "function") form.requestSubmit();
            else form.submit();
        });
    }
})();
