/**
 * picker.js —— 预览页面的「元素选取器」
 *
 * 由后端在编辑模式下注入到被预览的页面里：
 *   GET /pages/file/xxx.html?edit=1  →  在 </body> 前插入 <script src="/picker.js">
 *
 * 【为什么必须注入到页面内部】预览 iframe 带 sandbox 且**刻意不含
 * allow-same-origin**（这是安全边界，不能为了本功能拆掉），因此父页面
 * 拿不到它的 contentDocument。选取逻辑只能跑在页面**内部**，再用
 * postMessage 把结果回传给父页面。
 *
 * 【父页面如何鉴权】沙箱文档的 origin 是 'null'（不透明源），不能靠 origin
 * 判断来源；父页面用 event.source === iframe.contentWindow 校验。
 *
 * 【选取模式下页面自身不可交互】点击/按下/双击会被本脚本拦截并阻止冒泡
 * （与 Chrome DevTools 的选取行为一致），退出编辑模式后即恢复。
 *
 * 回传消息：
 *   {type:'dsh-picker:pick',   data:{label, width, height, ...}}  选中元素
 *   {type:'dsh-picker:cancel', data:{}}                           按下 Esc
 */
(function () {
  'use strict';

  var MSG_PICK = 'dsh-picker:pick';
  var MSG_CANCEL = 'dsh-picker:cancel';
  var Z = 2147483000;          // 压在页面绝大多数浮层之上，但仍低于 DevTools 自身

  var hovered = null;
  var boxEl = null;
  var tipEl = null;

  function post(type, data) {
    try {
      window.parent.postMessage({ type: type, data: data || {} }, '*');
    } catch (e) {
      // 极少数环境下 postMessage 可能被拒；绝不能因此影响被预览的页面本身
    }
  }

  function isOwn(el) {
    return !!(el && el.getAttribute && el.getAttribute('data-dsh-picker') !== null);
  }

  function classList(el) {
    var c = el.className;
    if (typeof c === 'string') {
      return c.trim().split(/\s+/).filter(Boolean);
    }
    // SVG 元素的 className 是 SVGAnimatedString
    if (c && typeof c.baseVal === 'string') {
      return c.baseVal.trim().split(/\s+/).filter(Boolean);
    }
    return [];
  }

  /** 元素标识，与 DevTools 提示框一致：tag#id.class1.class2 */
  function label(el) {
    var s = el.nodeName.toLowerCase();
    if (el.id) s += '#' + el.id;
    var cls = classList(el);
    if (cls.length) s += '.' + cls.slice(0, 4).join('.');
    return s;
  }

  function textOf(el) {
    var t = (el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!t) {
      var tag = el.nodeName;
      if (tag === 'IMG') t = el.getAttribute('alt') || '';
      else if (tag === 'INPUT' || tag === 'TEXTAREA') t = el.getAttribute('placeholder') || el.value || '';
    }
    return t.length > 120 ? t.slice(0, 120) + '…' : t;
  }

  function describe(el) {
    var r = el.getBoundingClientRect();
    return {
      label: label(el),
      tag: el.nodeName.toLowerCase(),
      id: el.id || '',
      classes: classList(el),
      width: Math.round(r.width),
      height: Math.round(r.height),
      text: textOf(el)
    };
  }

  function ensureOverlay() {
    var root = document.body || document.documentElement;
    if (!root) return;

    if (!boxEl) {
      boxEl = document.createElement('div');
      boxEl.setAttribute('data-dsh-picker', 'box');
      boxEl.style.cssText = 'position:fixed;z-index:' + (Z + 1) + ';pointer-events:none;' +
        'border:1px solid #2563eb;background:rgba(37,99,235,.14);border-radius:2px;' +
        'box-sizing:border-box;margin:0;padding:0;display:none;transition:none';
    }
    if (!tipEl) {
      tipEl = document.createElement('div');
      tipEl.setAttribute('data-dsh-picker', 'tip');
      tipEl.style.cssText = 'position:fixed;z-index:' + (Z + 2) + ';pointer-events:none;' +
        'background:#2563eb;color:#fff;font:11px/1.5 ui-monospace,Consolas,monospace;' +
        'padding:2px 6px;border-radius:3px;white-space:nowrap;max-width:96vw;' +
        'overflow:hidden;text-overflow:ellipsis;box-sizing:border-box;margin:0;' +
        'display:none;transition:none';
    }
    // 页面可能在运行中替换 body 内容，这里按需重新挂上
    if (!root.contains(boxEl)) root.appendChild(boxEl);
    if (!root.contains(tipEl)) root.appendChild(tipEl);
  }

  function paint(el) {
    ensureOverlay();
    if (!boxEl || !tipEl) return;
    var r = el.getBoundingClientRect();

    boxEl.style.display = 'block';
    boxEl.style.left = r.left + 'px';
    boxEl.style.top = r.top + 'px';
    boxEl.style.width = Math.max(0, r.width) + 'px';
    boxEl.style.height = Math.max(0, r.height) + 'px';

    var text = label(el) + '  ' + Math.round(r.width) + ' × ' + Math.round(r.height);
    tipEl.textContent = text;

    // 标签优先贴在元素上方；顶部放不下就压到元素内部上沿
    var h = 20;
    var top = r.top - h - 2;
    if (top < 2) top = Math.min(window.innerHeight - h, r.top + 2);
    tipEl.style.display = 'block';
    tipEl.style.top = top + 'px';
    tipEl.style.left = Math.max(2, Math.min(r.left, window.innerWidth - 60)) + 'px';
  }

  function hide() {
    if (boxEl) boxEl.style.display = 'none';
    if (tipEl) tipEl.style.display = 'none';
    hovered = null;
  }

  /** 拦住页面自身的交互（选取模式下点按钮不该触发页面逻辑） */
  function swallow(e) {
    if (isOwn(e.target)) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.stopImmediatePropagation) e.stopImmediatePropagation();
  }

  function onMove(e) {
    var el = e.target;
    if (!el || el.nodeType !== 1 || isOwn(el)) return;
    hovered = el;
    paint(el);
  }

  function onClick(e) {
    if (isOwn(e.target)) return;
    swallow(e);
    var el = e.target && e.target.nodeType === 1 ? e.target : hovered;
    if (!el) return;
    hovered = el;
    paint(el);
    post(MSG_PICK, describe(el));
  }

  function onKeyDown(e) {
    if (e.key === 'Escape' || e.keyCode === 27) {
      swallow(e);
      hide();
      post(MSG_CANCEL, {});
    }
  }

  function onViewportChange() {
    if (hovered && document.contains(hovered)) paint(hovered);
    else hide();
  }

  document.addEventListener('mousemove', onMove, true);
  document.addEventListener('mouseover', onMove, true);
  document.addEventListener('mousedown', swallow, true);
  document.addEventListener('mouseup', swallow, true);
  document.addEventListener('dblclick', swallow, true);
  document.addEventListener('click', onClick, true);
  document.addEventListener('keydown', onKeyDown, true);
  window.addEventListener('scroll', onViewportChange, true);
  window.addEventListener('resize', onViewportChange, true);
  document.addEventListener('mouseleave', hide, true);

  // 整页十字光标：让「选取模式」一眼可见（页面自己的 cursor 规则会被这条覆盖）
  try {
    var cursorStyle = document.createElement('style');
    cursorStyle.setAttribute('data-dsh-picker', 'cursor');
    cursorStyle.textContent = '*{cursor:crosshair !important}';
    (document.head || document.documentElement).appendChild(cursorStyle);
  } catch (e) { /* 纯装饰，失败也无所谓 */ }
})();
