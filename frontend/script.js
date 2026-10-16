const { createApp } = Vue;

// 侧边栏每个工作区默认显示的会话条数（对齐 DSH 的 COLLAPSED_SESSION_LIMIT）。
// 点「显示更多」每次再多放这么多条。
const COLLAPSED_SESSION_LIMIT = 5;

// marked 的 highlight 回调在 v8 已被移除（改为 marked-highlight 扩展）。
// 这里不再传 highlight，改为渲染完成后用 highlight.js 手动高亮 <pre><code>。
marked.setOptions({
    breaks: true,
    gfm: true
});

// 只允许安全协议，阻断 javascript: / vbscript: 等
function safeUrl(u) {
    if (!u) return '#';
    const s = String(u).trim().toLowerCase();
    if (s.startsWith('javascript:') || s.startsWith('vbscript:') ||
        s.startsWith('file:') || s.startsWith('data:text/html')) {
        return '#';
    }
    return u;
}

/**
 * 净化 HTML。
 * 所有进入 v-html 的内容都必须先过这里 —— 模型回复里包含联网搜索结果、
 * 知识库文档正文与文件内容，都属于不可信输入。
 */
function sanitizeHtml(html) {
    if (typeof DOMPurify === 'undefined') {
        // 兜底：净化库缺失时退化为纯文本，宁可丢格式也不要执行脚本
        console.error('DOMPurify 未加载，已降级为纯文本渲染');
        return String(html).replace(/[&<>"']/g, c => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
        ));
    }
    return DOMPurify.sanitize(html, {
        ADD_TAGS: ['semantics', 'annotation', 'mrow', 'mi', 'mo', 'mn', 'msup',
                   'msub', 'mfrac', 'msqrt', 'mtext', 'mspace', 'mover', 'munder',
                   'mtable', 'mtr', 'mtd', 'svg', 'path', 'line', 'g', 'use', 'defs'],
        ADD_ATTR: ['encoding', 'xmlns', 'viewBox', 'd', 'fill', 'stroke',
                   'stroke-width', 'x1', 'y1', 'x2', 'y2', 'transform',
                   'width', 'height', 'preserveAspectRatio', 'aria-hidden',
                   'focusable', 'xlink:href'],
        ALLOW_DATA_ATTR: false,
        FORBID_TAGS: ['style', 'form', 'input', 'button', 'iframe', 'object',
                      'embed', 'base', 'meta', 'link', 'script'],
        FORBID_ATTR: ['onerror', 'onload', 'onclick', 'onmouseover', 'onfocus',
                      'onblur', 'onchange', 'onsubmit', 'srcdoc', 'formaction']
    });
}

const API = window.location.origin && window.location.origin.startsWith('http')
    ? window.location.origin
    : 'http://localhost:8001';

const __app = createApp({
    data() {
        return {
            userInput: '',
            messages: [],
            sessionId: null,
            sessions: [],
            isLoading: false,
            loadingLabel: '正在思考...',
            fileContents: [],
            sidebarOpen: false,
            sidebarCollapsed: false,
            showConfirm: false,
            confirmTitle: '',
            confirmDesc: '',
            confirmBtnText: '确认删除',
            confirmCallback: null,
            previewImg: '',
            isDark: false,
            dragOver: false,
            msgKeyCounter: 0,
            sidebarWidth: parseInt(localStorage.getItem('sidebarWidth')) || 280,
            resizing: false,
            menuSessionId: null,
            menuPos: {},
            renamingId: null,
            renameValue: '',
            API: API,
            currentView: 'chat',
            kbDocuments: [],
            kbDragOver: false,
            kbViewDoc: null,

            // ── 页面预览（当前工作区 pages/ 目录，由 Agent 的 create_page 生成）──
            pagesList: [],
            pagesLoading: false,
            pagesDir: '',
            pagesWorkspace: '',
            pagePreviewName: '',
            // 递增即让 iframe 重新挂载 —— 预览必须显示最新内容，
            // 光改 src 相同地址时浏览器可能不重新加载
            pagePreviewKey: 0,
            // 元素选取（「编辑页面」）模式：开启后预览地址带 ?edit=1，
            // 后端会往页面里注入 picker.js
            pageEditMode: false,
            // 选中的元素（标签）：每选一个元素 push 一条，可多个并存、可单独删除。
            // 发送时按条转成「【页面编辑】…」文本块交给 agent（见 buildPickBlocks）。
            pagePicks: [],
            _pickSeq: 0,
            // 页面列表收起/展开（收起后预览区占满整栏高度），状态记忆在本地
            pagesListCollapsed: localStorage.getItem('pagesListCollapsed') === '1',

            // ── 右侧面板：页面预览 / 工作区文件（与对话并存，不再整页切换）──
            rightPanelOpen: false,
            rightPanelTab: 'pages',        // pages | files
            rightPanelWidth: parseInt(localStorage.getItem('rightPanelWidth')) || 520,
            // 窄屏（≤768px）时左右两侧都是抽屉；做成响应式数据，
            // 这样按钮高亮与点击行为都不会读到过期的 window.innerWidth
            isNarrow: window.innerWidth <= 768,
            // 工作区文件预览（只读文本）
            filePreview: null,
            filePreviewLoading: false,
            filePreviewError: '',
            selectedFilePath: '',

            // ── 工作区 ──
            workspaces: { list: [], active_id: null, active: null, base_dir: '' },
            wsMenuOpen: false,
            wsPickerOpen: false,
            wsBrowseData: { path: '', parent: null, entries: [], is_root: true },
            wsPathInput: '',
            wsSelectedPath: '',
            wsNewName: '',
            wsNewFolderName: '',
            wsLoading: false,
            wsCreating: false,
            wsPickMode: 'folder',
            wsSelectedFiles: [],
            // 折叠的工作区（默认全部展开）
            wsCollapsed: {},
            // 各工作区的对话：wsId -> [session, ...]
            wsSessions: {},
            // 对话搜索关键字
            convQuery: '',
            convSearchOpen: false,
            // 工作区操作菜单 / 重命名
            wsMenuFor: null,
            wsMenuPos: {},
            wsRenamingId: null,
            wsRenameValue: '',
            // 每个工作区展开显示的会话条数（默认 5，点「显示更多」每次 +5）
            wsReveal: {},
            // 视图选项：排序方式 / 是否显示已归档
            wsOrderBy: 'updated',       // updated | manual
            wsShowArchived: false,
            wsViewMenuOpen: false,
            wsViewMenuPos: {},

            // ── 工作区文件树（相对当前工作区根）──
            workspace: { name: '', path: '', count: 0, truncated: false },
            // 缓存各目录已加载的子节点：path -> [node, ...]
            // 刻意不做成响应式（用普通对象），靠 treeVersion 触发更新
            treeStore: { '': [] },
            treeVersion: 0,
            treeLoading: {},        // path -> true 表示正在加载
            expandedPaths: {},      // path -> true 表示已展开
            treeFilter: '',         // 树内搜索关键字
            // 当前对话所属作用域：'' = 工作区根，否则是相对工作区根的路径
            currentScope: '',
            // ── 设置面板 ──
            settingsOpen: false,
            settingsTab: 'general',
            settingsTabs: [
                { id: 'general', label: '通用设置', icon: 'fa-solid fa-gear' },
                { id: 'model',   label: '模型',     icon: 'fa-solid fa-cube' },
                { id: 'plugins', label: '插件',     icon: 'fa-solid fa-list-ul' },
                { id: 'agent',   label: 'Agent 预设', icon: 'fa-solid fa-robot' },
            ],
            themeChoices: [
                { id: 'light',  label: '浅色',    icon: 'fa-solid fa-sun' },
                { id: 'dark',   label: '深色',    icon: 'fa-solid fa-moon' },
                { id: 'system', label: '跟随系统', icon: 'fa-solid fa-desktop' },
            ],
            // 'light' | 'dark' | 'system'，与 settings 分开存以便跟随系统时保留原选择
            themeMode: 'light',
            systemPrefersDark: false,
            settings: {
                permission: 'workspace',
                language: 'zh',
                fontSize: 16,
                density: 'compact',
                busySend: 'queue',
            },
            // 运行时只读信息（从后端 /kb/* 等接口汇总）
            runtimeInfo: {
                model: '', temperature: '', embedding: '',
                kbDocs: 0, kbChunks: 0, tools: null,
                // 接口配置（密钥只回显首尾，完整密钥不出后端）
                base_url: '', api_key_masked: '', api_key_set: false,
                writable: true, env_file: '',
            },
            // 「模型」页里换 API 的表单状态
            apiForm: { api_key: '', base_url: '' },
            apiSaving: false,
            apiKeyVisible: false,
            apiSaveHint: '保存前会先做一次真实连通测试，通过才生效',
            apiHintClass: '',
            plugins: [
                { name: 'tavily_search', label: '联网搜索', desc: 'Tavily 搜索，获取实时信息', on: true },
                { name: 'search_doc', label: '知识库检索（平衡）', desc: '向量 + BM25 + RRF 融合 + 重排', on: true },
                { name: 'search_doc_fast', label: '知识库检索（快速）', desc: '仅向量检索，低延迟', on: true },
                { name: 'search_doc_quality', label: '知识库检索（深度）', desc: '查询改写 + 多查询 + HyDE', on: true },
                { name: 'execute_code', label: '代码执行', desc: '子进程隔离执行 Python，可生成图表', on: true },
                { name: 'run_shell', label: '命令行执行', desc: 'bash（无则降级 PowerShell），超时 60 秒', on: true },
                { name: 'read_file', label: '读取文件', desc: '读取工作目录内的文本/PDF/Office 文件', on: true },
                { name: 'write_file', label: '写入文件', desc: '在工作目录内创建或覆盖文件', on: true },
                { name: 'edit_file', label: '编辑文件', desc: '对已有文件做精确文本替换', on: true },
                { name: 'todo', label: '任务规划', desc: '多步骤任务的待办列表跟踪', on: true },
                { name: 'task_*', label: '持久化任务', desc: '跨会话任务与依赖关系管理', on: true },
            ],
        };
    },

    computed: {
        currentTitle() {
            if (this.messages.length === 0) return '新对话';
            const f = this.messages.find(m => m.isUser);
            if (f && f.text) {
                const t = f.text.replace(/<[^>]*>/g, '').trim();
                return t.length > 30 ? t.slice(0, 30) + '...' : t;
            }
            return '新对话';
        },
        pinnedSessions() {
            return this.sessions.filter(s => s.pinned).sort((a, b) => b.createdAt - a.createdAt);
        },

        /** 依赖 treeVersion 以便树数据变化时重算 */
        workspaceNodes() {
            void this.treeVersion;
            return this.treeStore[''] || [];
        },

        currentNode() {
            void this.treeVersion;
            return this.findNode(this.currentScope);
        },

        currentScopeLabel() {
            if (!this.currentScope) return this.workspace.name || '工作区';
            const n = this.findNode(this.currentScope);
            return n ? n.name : this.currentScope;
        },

        activeWorkspace() {
            return this.workspaces.list.find(w => w.id === this.workspaces.active_id) || null;
        },

        // ── 页面预览相关 ──
        pagePreviewUrl() {
            if (!this.pagePreviewName) return '';
            // 逐段编码：页面名可能含子目录（demo/index.html）
            const path = this.pagePreviewName.split('/').map(encodeURIComponent).join('/');
            // 编辑模式才带 edit=1（后端据此注入元素选取脚本）
            const edit = (this.pageEditMode && this.isPreviewablePage) ? '?edit=1' : '';
            return API + '/pages/file/' + path + edit;
        },

        pagePreviewExt() {
            const n = this.pagePreviewName || '';
            const i = n.lastIndexOf('.');
            return i >= 0 ? n.slice(i).toLowerCase() : '';
        },

        isPreviewablePage() {
            return ['.html', '.htm'].includes(this.pagePreviewExt);
        },

        isPreviewableImage() {
            return ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.ico'].includes(this.pagePreviewExt);
        },

        canPreviewFrame() {
            return this.isPreviewablePage || this.isPreviewableImage;
        },

        pagesWorkspaceLabel() {
            const ws = this.pagesWorkspace || (this.activeWorkspace ? this.activeWorkspace.name : '');
            return ws ? ('当前工作区：' + ws + ' · 页面存放于 pages/ 目录')
                      : '页面存放于当前工作区的 pages/ 目录';
        },

        /** 左侧边栏当前是否可见（顶部左侧按钮的高亮依据） */
        leftSidebarOpen() {
            return this.isNarrow ? this.sidebarOpen : !this.sidebarCollapsed;
        },

        /**
         * 文件树摊平成可见行（含缩进层级）。
         *
         * 刻意不做递归组件：树的数据本来就按 path 分组存在 treeStore 里，
         * 展开状态又在 expandedPaths 里，摊平只是一次遍历 —— 比引入
         * 递归组件（还要把 toggleDir 等父级方法透传下去）简单得多。
         */
        visibleTreeRows() {
            void this.treeVersion;
            const rows = [];
            const walk = (parentPath, depth) => {
                for (const node of (this.treeStore[parentPath] || [])) {
                    rows.push({ node, depth });
                    if (node.type === 'dir' && this.expandedPaths[node.path]) {
                        walk(node.path, depth + 1);
                    }
                }
            };
            walk('', 0);
            return rows;
        },

        /**
         * 侧边栏主体：工作区列表，每个工作区下面挂它自己的对话。
         *
         * 对齐 DSH 的浏览行为：
         *  - 已归档的会话单独分区，不混在正常列表里
         *  - 每个工作区默认只显示前 5 条空闲会话，点「显示更多」每次多放 5 条
         *  - 搜索时跨工作区过滤，命中的工作区自动展开
         *  - 排序：已固定的在前，其余按最近更新（或手动顺序）
         */
        workspaceGroups() {
            void this._wsSessionsVersion;   // 依赖：会话缓存变化时重算
            const q = (this.convQuery || '').trim().toLowerCase();
            const out = [];

            for (const w of this.workspaces.list) {
                const all = (this.wsSessions[w.id] || []).filter(s => !s.archived);

                let matched = all;
                if (q) {
                    const wsHit = (w.name || '').toLowerCase().includes(q) ||
                                  (w.path || '').toLowerCase().includes(q);
                    matched = wsHit ? all : all.filter(s =>
                        (s.title || '').toLowerCase().includes(q));
                    if (wsHit || matched.length === 0) {
                        // 工作区名/路径命中、或该工作区下没有匹配项，
                        // 都要保留这一组（后者用空态提示"没有匹配的对话"）
                    }
                }
                // 搜索时一律展开
                const collapsed = q ? false : !!this.wsCollapsed[w.id];

                // 排序：固定的在前，其余按最近更新；手动模式下按保存的顺序
                const pinned = matched.filter(s => s.pinned);
                let rest = matched.filter(s => !s.pinned);
                if (this.wsOrderBy === 'manual') {
                    rest = this.sortByManual(w.id, rest);
                } else {
                    rest = rest.slice().sort((a, b) => (b.updatedAt || b.createdAt || 0)
                                                   - (a.updatedAt || a.createdAt || 0));
                }
                const ordered = pinned.concat(rest);

                out.push({
                    id: w.id,
                    name: w.name,
                    path: w.path,
                    exists: w.exists !== false,
                    active: w.id === this.workspaces.active_id,
                    collapsed,
                    visible: collapsed ? [] : ordered,
                    overflow: collapsed ? 0 : ordered.length,
                    total: all.length,
                    matchedCount: matched.length,
                    latest: ordered.reduce((m, s) => Math.max(m, s.createdAt || 0), 0),
                });
            }

            // 有对话的排前面，其次按最近使用（激活的工作区不再强制置顶 ——
            // DSH 也是按最近更新排，否则切来切去列表会跳）
            out.sort((a, b) => {
                if ((a.total > 0) !== (b.total > 0)) return a.total > 0 ? -1 : 1;
                if (a.latest !== b.latest) return b.latest - a.latest;
                return (a.name || '').localeCompare(b.name || '');
            });
            return out;
        },

        /**
         * 侧边栏渲染用的扁平行列表。
         *
         * 为什么不在模板里嵌套两层 v-for：工作区行与它的会话行之间要插入
         * 「显示更多 / 收起」这类兄弟节点，平坦化之后控制更直观。
         */
        sidebarRows() {
            const rows = [];
            const q = this.wsSearchActive;
            for (const g of this.workspaceGroups) {
                rows.push({ kind: 'ws', key: 'ws-' + g.id, ws: g });
                if (g.collapsed) continue;

                const limit = q ? Infinity : (this.wsReveal[g.id] || COLLAPSED_SESSION_LIMIT);
                const shown = g.visible.slice(0, limit);
                for (const s of shown) {
                    rows.push({ kind: 'session', key: 's-' + s.id, session: s, ws: g });
                }
                if (g.visible.length === 0) {
                    rows.push({ kind: 'empty', key: 'e-' + g.id, ws: g, text: q ? '没有匹配的对话' : '暂无对话' });
                } else if (g.visible.length > limit) {
                    const more = Math.min(COLLAPSED_SESSION_LIMIT, g.visible.length - limit);
                    rows.push({
                        kind: 'more', key: 'm-' + g.id, ws: g,
                        label: `显示其余 ${more} 个会话`,
                    });
                } else if (!q && (this.wsReveal[g.id] || 0) > COLLAPSED_SESSION_LIMIT) {
                    rows.push({ kind: 'less', key: 'l-' + g.id, ws: g, label: '收起' });
                }
            }
            return rows;
        },

        /** 已归档的会话（单独分区，不混在正常列表里） */
        archivedSessions() {
            void this._wsSessionsVersion;
            const out = [];
            for (const w of this.workspaces.list) {
                for (const s of (this.wsSessions[w.id] || [])) {
                    if (s.archived) out.push({ ...s, _wsName: w.name });
                }
            }
            return out.sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
        },

        /** 侧边栏里的会话总数（排除归档） */
        activeSessionCount() {
            void this._wsSessionsVersion;
            let n = 0;
            for (const w of this.workspaces.list) {
                n += (this.wsSessions[w.id] || []).filter(s => !s.archived).length;
            }
            return n;
        },

        wsSearchActive() {
            return !!(this.convQuery || '').trim();
        },

        wsDefaultName() {
            if (this.wsPickMode === 'files') {
                if (!this.wsSelectedFiles.length) return '给工作区起个名字（可留空）';
                if (this.wsSelectedFiles.length === 1) {
                    const p = this.wsSelectedFiles[0];
                    const parts = p.replace(/[\\/]+$/, '').split(/[\\/]/);
                    return parts[parts.length - 1] || '文件工作区';
                }
                return `${this.wsSelectedFiles.length} 个文件`;
            }
            if (!this.wsSelectedPath) return '给工作区起个名字（可留空取文件夹名）';
            const parts = this.wsSelectedPath.replace(/[\\/]+$/, '').split(/[\\/]/);
            return parts[parts.length - 1] || '工作区';
        },
    },

    async created() {
        this.loadTheme();
        this.loadSettings();
        // 必须先拿到工作区列表 —— 会话记录的存储键依赖 active_id
        await this.loadWorkspaces();
        this.migrateLegacySessions();
        this.loadSessions();
        // 加载当前工作区的目录树
        await this.loadTree('');
        this.$nextTick(() => {
            document.documentElement.style.setProperty('--sidebar-w', this.sidebarWidth + 'px');
        });
        const ls = localStorage.getItem(this.sessionsKey('lastSessionId'));
        if (ls) {
            const s = this.sessions.find(x => x.id === ls);
            if (s) {
                this.sessionId = s.id;
                this.messages = s.messages.map(m => ({ ...m }));
                this.currentScope = s.scope || '';
            }
        }
    },

    mounted() {
        this.autoResize();

        // 「跟随系统」主题：监听系统配色变化
        if (window.matchMedia) {
            this._mq = window.matchMedia('(prefers-color-scheme: dark)');
            this._onSystemTheme = e => {
                this.systemPrefersDark = e.matches;
                if (this.themeMode === 'system') this.applyTheme();
            };
            if (this._mq.addEventListener) this._mq.addEventListener('change', this._onSystemTheme);
            else if (this._mq.addListener) this._mq.addListener(this._onSystemTheme);
        }

        // 事件处理器保存为具名函数，便于在 beforeUnmount 中移除。
        // 早期版本用匿名函数注册且从不移除。
        this._onKeydown = e => {
            if (e.key === 'Escape') {
                // 设置面板优先级最高，先关它
                if (this.settingsOpen) { this.closeSettings(); return; }
                if (this.previewImg) this.previewImg = '';
                if (this.menuSessionId) this.closeMenu();
                if (this.renamingId) this.cancelRename();
                // 右侧面板最后关（它常驻在对话旁边，不像弹层那样抢焦点）
                if (this.rightPanelOpen) this.rightPanelOpen = false;
            }
        };
        this._onDocClick = e => {
            if (this.menuSessionId && !e.target.closest('.session-menu') && !e.target.closest('.sess-more')) {
                this.closeMenu();
            }
        };
        this._onMouseUp = () => {
            // 鼠标在窗口外松开时也要结束拖拽，否则会残留
            // user-select:none 与 col-resize 光标
            if (this.resizing) {
                this.resizing = false;
                document.body.style.userSelect = '';
                document.body.style.cursor = '';
            }
        };

        document.addEventListener('keydown', this._onKeydown);
        document.addEventListener('click', this._onDocClick);
        window.addEventListener('mouseup', this._onMouseUp);
        window.addEventListener('blur', this._onMouseUp);
        // 窗口跨过 768px 时切换「抽屉 / 收窄」两套行为（按钮高亮依赖它）
        this._onResize = () => { this.isNarrow = window.innerWidth <= 768; };
        this.isNarrow = window.innerWidth <= 768;
        window.addEventListener('resize', this._onResize);

        // 预览页里的 picker.js 通过 postMessage 回传选中元素。
        //
        // 【鉴权】沙箱文档的 origin 是 'null'（不透明源），靠 origin 判断不可行；
        // 只接受「当前预览 iframe」这个窗口发来的消息：event.source 必须等于
        // iframe.contentWindow。页面自己伪造的消息只可能产出错误的前缀文本，
        // 这里仍严格校验，避免任何页面都能往输入框里塞内容。
        this._onPickerMessage = e => {
            const f = this.$refs.pageFrame;
            if (!f || e.source !== f.contentWindow) return;
            const d = e.data || {};
            if (d.type === 'dsh-picker:pick' && d.data) this.onElementPicked(d.data);
            else if (d.type === 'dsh-picker:cancel') this.exitPageEditMode();
        };
        window.addEventListener('message', this._onPickerMessage);
    },

    beforeUnmount() {
        if (this._onKeydown) document.removeEventListener('keydown', this._onKeydown);
        if (this._onDocClick) document.removeEventListener('click', this._onDocClick);
        if (this._onResize) window.removeEventListener('resize', this._onResize);
        if (this._onPickerMessage) window.removeEventListener('message', this._onPickerMessage);
        if (this._onMouseUp) {
            window.removeEventListener('mouseup', this._onMouseUp);
            window.removeEventListener('blur', this._onMouseUp);
        }
        if (this._mq && this._onSystemTheme) {
            if (this._mq.removeEventListener) this._mq.removeEventListener('change', this._onSystemTheme);
            else if (this._mq.removeListener) this._mq.removeListener(this._onSystemTheme);
        }
    },

    watch: {
        // 消息稳定后补一次代码高亮（流式期间反复高亮代价太高）
        messages: {
            handler() {
                if (!this.isLoading) this.highlightCodeBlocks();
            },
            deep: true
        },
        // 设置变更后持久化，并应用字号
        settings: {
            handler(v) {
                this.normalizeSettings();
                this.applyFontSize(v.fontSize);
                this.saveSettings();
            },
            deep: true
        },
        // 面板关掉就退出元素选取：下次打开是干净状态，
        // 也避免 iframe 带着 ?edit=1 重新挂载
        rightPanelOpen(v) {
            if (!v) this.pageEditMode = false;
        },
    },

    methods: {
        renderMarkdown(t) {
            if (!t) return '';
            try {
                let mathBlocks = [];
                let mathIndex = 0;

                // 先摘出公式再交给 marked，避免 $...$ 被当作 Markdown 破坏。
                // 提醒：此处不区分代码块，代码里的 $ 也会被摘出（已知取舍）。
                t = t.replace(/\$\$([\s\S]+?)\$\$/g, (m, tex) => {
                    const key = 'MATHBLK' + mathIndex + 'XYZ';
                    mathBlocks.push({ key, tex: tex.trim(), display: true });
                    mathIndex++;
                    return key;
                });
                t = t.replace(/\\\[([\s\S]+?)\\\]/g, (m, tex) => {
                    const key = 'MATHBLK' + mathIndex + 'XYZ';
                    mathBlocks.push({ key, tex: tex.trim(), display: true });
                    mathIndex++;
                    return key;
                });
                t = t.replace(/\\\(([\s\S]+?)\\\)/g, (m, tex) => {
                    const key = 'MATHINL' + mathIndex + 'XYZ';
                    mathBlocks.push({ key, tex: tex.trim(), display: false });
                    mathIndex++;
                    return key;
                });
                t = t.replace(/\$([^\$\n]+?)\$/g, (m, tex) => {
                    if (/^[a-zA-Z0-9\s\.,;:=+\-*/^_{}()\[\]\\]+$/.test(tex)) {
                        const key = 'MATHINL' + mathIndex + 'XYZ';
                        mathBlocks.push({ key, tex: tex.trim(), display: false });
                        mathIndex++;
                        return key;
                    }
                    return m;
                });

                let h = marked.parse(t);
                if (typeof h !== 'string') return this.escapeHtml(t);

                // 把后端返回的相对路径补成完整地址
                h = h.replace(/(src|href)="\/(images|files)\/([^"]+)"/g, (m, a, b, c) => a + '="' + this.API + '/' + b + '/' + c + '"');
                h = h.replace(/(src|href)="(?!https?:|data:)([^"]+)"/g, (m, a, b) => {
                    if (b.startsWith('/')) return a + '="' + this.API + b + '"';
                    return m;
                });

                if (window.katex && mathBlocks.length) {
                    for (const mb of mathBlocks) {
                        try {
                            // trust: true 会放行 \href / \url / \includegraphics，
                            // 等于给公式开了任意链接与资源加载的口子，必须关掉。
                            const rendered = window.katex.renderToString(mb.tex, {
                                displayMode: mb.display,
                                throwOnError: false,
                                trust: false,
                                strict: 'ignore'
                            });
                            h = h.split(mb.key).join(rendered);
                        } catch (e) {
                            h = h.split(mb.key).join('<code>' + this.escapeHtml(mb.tex) + '</code>');
                        }
                    }
                } else {
                    // 无 KaTeX 时必须转义，否则公式里的 HTML 会直接被注入
                    for (const mb of mathBlocks) {
                        const tag = mb.display ? 'div' : 'span';
                        const cls = mb.display ? 'math-block' : 'math-inline';
                        h = h.split(mb.key).join(
                            '<' + tag + ' class="' + cls + '">' + this.escapeHtml(mb.tex) + '</' + tag + '>'
                        );
                    }
                }

                // 统一净化：所有 v-html 的内容出口
                h = sanitizeHtml(h);

                // 净化后再修链接协议（防止净化被绕过）
                h = h.replace(/(href|src)="([^"]*)"/g, (m, a, b) => a + '="' + safeUrl(b) + '"');

                return h;
            } catch (e) {
                console.error('renderMarkdown failed:', e);
                return this.escapeHtml(t);
            }
        },

        escapeHtml(s) {
            return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
                { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
            ));
        },

        /** 渲染后对代码块做语法高亮（marked 不再提供 highlight 回调） */
        highlightCodeBlocks() {
            if (typeof hljs === 'undefined') return;
            this.$nextTick(() => {
                document.querySelectorAll('pre code:not([data-highlighted])').forEach(el => {
                    try {
                        hljs.highlightElement(el);
                        el.setAttribute('data-highlighted', '1');
                    } catch (e) { /* 高亮失败不影响展示 */ }
                });
            });
        },

        /** 点击消息气泡里的图片时放大预览（styles.css 已设置 cursor:pointer） */
        onBubbleClick(ev) {
            const el = ev.target;
            if (el && el.tagName === 'IMG' && el.src) {
                this.previewImg = el.src;
            }
        },

        autoResize() {
            this.$nextTick(() => {
                const el = this.$refs.chatInput;
                if (el) {
                    el.style.height = 'auto';
                    el.style.height = Math.min(el.scrollHeight, 150) + 'px';
                }
            });
        },

        triggerFileUpload() {
            this.$refs.fileInput.click();
        },

        onFileSelected(e) {
            const f = Array.from(e.target.files);
            this.readFiles(f);
            e.target.value = '';
        },

        onDrop(e) {
            this.dragOver = false;
            const f = Array.from(e.dataTransfer.files);
            if (f.length > 0) this.readFiles(f);
        },

        onPaste(e) {
            const items = e.clipboardData ? e.clipboardData.items : null;
            if (!items) return;
            const imgs = [];
            for (const item of items) {
                if (item.type.startsWith('image/')) {
                    const file = item.getAsFile();
                    if (file) imgs.push(file);
                }
            }
            if (imgs.length > 0) {
                e.preventDefault();
                this.readFiles(imgs);
            }
        },

        readFiles(files) {
            const ext = ['.py', '.js', '.html', '.css', '.json', '.md', '.txt', '.csv', '.xml', '.yaml', '.yml', '.java', '.ts', '.rs', '.go', '.c', '.cpp', '.h', '.sql', '.sh', '.bat', '.env'];
            const imgExt = ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg', '.ico'];

            for (const f of files) {
                const e = '.' + f.name.split('.').pop().toLowerCase();
                const isImg = f.type.startsWith('image/') || imgExt.includes(e);

                if (isImg) {
                    const extName = f.name.split('.').pop().toLowerCase();
                    const isSvg = extName === 'svg';
                    const goodExt = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'ico'].includes(extName) ? extName : 'png';
                    const displayName = f.name && f.name.indexOf('.') > 0 ? f.name : `粘贴图片_${Date.now() % 1000}.${goodExt}`;

                    if (isSvg) {
                        const r = new FileReader();
                        r.onload = ev => {
                            this.fileContents.push({
                                name: displayName,
                                content: ev.target.result,
                                type: 'image',
                                size: f.size,
                                origSize: f.size
                            });
                        };
                        r.readAsText(f);
                    } else {
                        const processImg = (dataUrl) => {
                            const img = new Image();
                            img.onload = () => {
                                const maxW = 1920, maxH = 1080;
                                let w = img.width, h = img.height;
                                if (w > maxW || h > maxH) {
                                    const ratio = Math.min(maxW / w, maxH / h);
                                    w = Math.round(w * ratio);
                                    h = Math.round(h * ratio);
                                }
                                const canvas = document.createElement('canvas');
                                canvas.width = w;
                                canvas.height = h;
                                const ctx = canvas.getContext('2d');
                                ctx.drawImage(img, 0, 0, w, h);
                                const mime = f.type || ('image/' + goodExt);
                                const finalUrl = (w !== img.width || h !== img.height) ? canvas.toDataURL(mime, 0.92) : dataUrl;
                                this.fileContents.push({
                                    name: displayName,
                                    content: finalUrl,
                                    type: 'image',
                                    size: f.size,
                                    origSize: f.size
                                });
                            };
                            img.onerror = () => {
                                this.fileContents.push({
                                    name: displayName,
                                    content: dataUrl,
                                    type: 'image',
                                    size: f.size,
                                    origSize: f.size
                                });
                            };
                            img.src = dataUrl;
                        };
                        const r = new FileReader();
                        r.onload = ev => processImg(ev.target.result);
                        r.readAsDataURL(f);
                    }
                } else if (f.type.startsWith('text/') || ext.includes(e)) {
                    const r = new FileReader();
                    r.onload = ev => {
                        this.fileContents.push({
                            name: f.name,
                            content: ev.target.result,
                            type: 'text'
                        });
                    };
                    r.readAsText(f);
                } else {
                    this.fileContents.push({
                        name: f.name,
                        content: '[Binary: ' + f.name + ']',
                        type: 'binary'
                    });
                }
            }
        },

        removeFile(i) {
            this.fileContents.splice(i, 1);
        },

        formatSize(bytes) {
            if (!bytes) return '';
            if (bytes < 1024) return bytes + ' B';
            if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
            return (bytes / 1048576).toFixed(2) + ' MB';
        },

        async sendMessage() {
            const t = this.userInput.trim();
            const picks = this.pagePicks.slice();
            if ((!t && this.fileContents.length === 0 && picks.length === 0) || this.isLoading) return;

            let dt = t || '';
            // 元素标签 → 文本块，放在消息最前（每段自带「修改要求」，用户写的要求已嵌在里面）
            const pickBlocks = this.buildPickBlocks(t);
            if (pickBlocks) dt = pickBlocks;
            const hasFiles = this.fileContents.length > 0;
            const userAtts = this.fileContents.map(f => ({
                name: f.name,
                type: f.type,
                content: f.content,
                size: f.size
            }));

            for (const f of this.fileContents) {
                if (f.type === 'image') {
                    dt += '\n\n[图片: ' + f.name + ']\n' + f.content;
                } else {
                    dt += '\n\n[文件: ' + f.name + ']\n' + f.content;
                }
            }
            if (!dt.trim()) dt = '(上传文件)';

            const n = new Date();
            const ts = n.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });

            // 气泡里回显本次引用了哪些元素（发送正文是文本块，见 buildPickBlocks）；
            // 这个数组同时要落到会话存储里，刷新后才不会丢
            const userPicks = picks.length > 0 ? picks.map(p => ({
                page: p.page, label: p.label, width: p.width, height: p.height, text: p.text
            })) : null;

            this.messages.push({
                isUser: true,
                text: t || (hasFiles ? '(上传附件)' : (picks.length ? '(元素修改)' : '(空)')),
                time: ts,
                _key: 'm' + (this.msgKeyCounter++),
                userAtts: userAtts.length > 0 ? userAtts : null,
                userPicks: userPicks
            });

            const bi = this.messages.length;
            const botMsg = {
                isUser: false,
                text: '',
                streaming: true,
                toolStatus: 'thinking',
                time: ts,
                _key: 'm' + (this.msgKeyCounter++),
                workflowSteps: [],
                traceGroups: [],
                thinkings: [],
                traceCollapsed: false,
                thinkingCollapsed: true,
                traceTotalMs: 0,
                traceRounds: 0,
                traceToolTimeMs: 0,
                traceLlmTimeMs: 0,
                traceToolCalls: [],
            };
            this.messages.push(botMsg);

            this.userInput = '';
            this.fileContents = [];
            this.pagePicks = [];       // 元素标签已转成文本块进入消息，清空待输入区
            this.isLoading = true;
            this.loadingLabel = '正在初始化...';

            if (!this.sessionId) {
                this.sessionId = 's' + Date.now() + '_' + Math.random().toString(36).slice(2, 6);
            }

            // 用局部变量管理思考计时器：挂在消息对象上会随消息一起被
            // 序列化进 localStorage，且消息被替换时定时器仍会持续走。
            let thinkingTimer = null;

            const stopThinkingTimer = () => {
                if (thinkingTimer) {
                    clearInterval(thinkingTimer);
                    thinkingTimer = null;
                }
            };

            // 流中断保护：超时未收到任何数据则中止，避免界面永久卡在"生成中"
            const STREAM_IDLE_TIMEOUT = 180000;
            let idleTimer = null;
            let aborted = false;

            // 【必须在 try 之外声明】try / catch / finally 是**三个独立的块作用域**，
            // 在 try 里用 let 声明的变量，catch 与 finally 里是看不到的（会抛
            // ReferenceError）。这些变量三个块都要用：
            //   pendingTokenText / ft —— catch 与 finally 都要把待冲刷的 token 落进气泡
            // 曾经把它们声明在 try 内，导致流一结束 finally 首行就抛
            // "ReferenceError: pendingTokenText is not defined" ——
            // 于是 isLoading 永远清不掉，界面卡在"正在生成回复"，同时代码高亮与
            // 滚动收尾也全都没执行。实测由浏览器异常捕获定位到（script.js 收尾处）。
            let buf = '', ft = '', fa = null, finalReply = '';
            let gotDone = false;
            let pendingTokenText = '';

            const abortController = new AbortController();
            this._abortController = abortController;

            try {
                const r = await fetch(API + '/chat/stream', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        message: dt,
                        session_id: this.sessionId,
                        scope: this.currentScope || '',
                        workspace_id: this.workspaces.active_id || null,
                    }),
                    signal: abortController.signal,
                });

                // 必须有状态检查：429（限流）或 500 返回的是 JSON/HTML 而非 SSE，
                // 不做检查会被当成"零事件的流"，界面静默卡住且没有任何报错。
                if (!r.ok) {
                    let detail = '';
                    try { detail = (await r.text()).slice(0, 300); } catch (e) {}
                    throw new Error(`HTTP ${r.status}${detail ? ' - ' + detail : ''}`);
                }
                if (!r.body) throw new Error('响应不支持流式读取');

                const rd = r.body.getReader();
                const dec = new TextDecoder();
                // buf / ft / fa / finalReply / gotDone / pendingTokenText 已在 try 之外声明
                // （catch 与 finally 也要用，块作用域详见上面的注释）
                let tokenRafPending = false;
                const flushTokens = () => {
                    if (pendingTokenText) {
                        ft += pendingTokenText;
                        this.messages[bi].text = ft;
                        this.messages[bi].toolStatus = '';
                        pendingTokenText = '';
                    }
                    this.scrollToBottom();
                    tokenRafPending = false;
                };
                const scheduleTokenFlush = () => {
                    if (!tokenRafPending) {
                        tokenRafPending = true;
                        requestAnimationFrame(flushTokens);
                    }
                };

                const resetIdle = () => {
                    if (idleTimer) clearTimeout(idleTimer);
                    idleTimer = setTimeout(async () => {
                        aborted = true;
                        try { await rd.cancel(); } catch (e) {}
                    }, STREAM_IDLE_TIMEOUT);
                };
                resetIdle();

                while (true) {
                    const { done, value } = await rd.read();
                    if (done) break;
                    resetIdle();

                    buf += dec.decode(value, { stream: true });
                    const ls = buf.split('\n');
                    buf = ls.pop() || '';

                    for (const l of ls) {
                        if (!l.startsWith('data: ')) continue;
                        const d = l.slice(6).trim();
                        if (!d) continue;

                        try {
                            const e = JSON.parse(d);

                            if (e.type === 'phase') {
                                this.loadingLabel = e.data.label || '处理中...';
                                const phase = e.data.phase;
                                if (phase === 'init') {
                                    this.messages[bi].toolStatus = 'thinking';
                                    this.messages[bi].workflowSteps = [];
                                    this.messages[bi].traceGroups = [];
                                    this.messages[bi].traceTotalMs = 0;
                                } else if (phase === 'thinking') {
                                    if (!this.messages[bi].workflowSteps || this.messages[bi].workflowSteps.length === 0) {
                                        this.messages[bi].toolStatus = 'thinking';
                                    }
                                } else if (phase === 'processing') {
                                    this.messages[bi].toolStatus = 'calling';
                                } else if (phase === 'answering') {
                                    this.messages[bi].toolStatus = 'thinking';
                                } else if (phase === 'done') {
                                    this.messages[bi].toolStatus = '';
                                } else if (phase === 'multi_agent') {
                                    const sub = e.data.sub || '';
                                    if (sub === 'init' && !e.data.enabled) {
                                        this.messages[bi].toolStatus = 'thinking';
                                    }
                                    if (!this.messages[bi].workflowSteps) this.messages[bi].workflowSteps = [];
                                    this.messages[bi].workflowSteps.push({
                                        label: e.data.label || 'Multi-Agent',
                                        icon: sub.startsWith('planner') ? '📋'
                                              : sub.startsWith('executor') ? '🔧'
                                              : sub.startsWith('judge') ? '🧐'
                                              : sub === 'pass' ? '✅'
                                              : sub === 'replan' ? '🔁'
                                              : sub === 'max_retries' ? '⚠️'
                                              : '🧠',
                                        description: e.data.duration_ms ? `耗时 ${e.data.duration_ms}ms` : '',
                                        status: sub === 'pass' || sub === 'max_retries' ? 'done'
                                               : sub === 'replan' ? 'warning'
                                               : 'info',
                                        phaseSub: sub,
                                    });
                                }
                                this.scrollToBottom();

                            } else if (e.type === 'phase_end') {
                                const td = e.data.duration_ms || 0;
                                if (e.data.phase === 'thinking') {
                                    // 已经由 thinking_end 事件处理 duration 到 thinkingDuration
                                }
                                this.scrollToBottom();

                            } else if (e.type === 'batch_start') {
                                if (!this.messages[bi].traceGroups) this.messages[bi].traceGroups = [];
                                const batch = {
                                    round: e.data.round,
                                    label: `第 ${e.data.round} 轮 · 工具调用${e.data.is_parallel ? '（并行批次 ×' + e.data.tool_count + '）' : ''}`,
                                    isParallel: !!e.data.is_parallel,
                                    toolCount: e.data.tool_count,
                                    tools: [],
                                    status: 'running',
                                    startTime: e.data.ts,
                                };
                                this.messages[bi].traceGroups.push(batch);
                                this.scrollToBottom();

                            } else if (e.type === 'batch_end') {
                                const groups = this.messages[bi].traceGroups || [];
                                const g = groups[groups.length - 1];
                                if (g) {
                                    g.status = 'done';
                                    g.durationMs = e.data.batch_duration_ms;
                                }
                                this.scrollToBottom();

                            } else if (e.type === 'thinking_start') {
                                const phase = (this.messages[bi].traceGroups && this.messages[bi].traceGroups.length > 0)
                                    ? this.messages[bi].traceGroups.length + 1
                                    : 1;
                                const thinkIdx = this.messages[bi].thinkings.length;
                                this.messages[bi].thinkings.push({
                                    round: phase,
                                    text: '',
                                    done: false,
                                    collapsed: true,
                                    startTime: Date.now(),
                                    duration: 0,
                                    durationTimer: null,
                                    idx: thinkIdx,
                                });
                                const cur = this.messages[bi].thinkings[thinkIdx];
                                cur.durationTimer = setInterval(() => {
                                    if (cur.done) return;
                                    cur.duration = Math.round((Date.now() - cur.startTime) / 1000);
                                }, 200);
                                this.messages[bi].thinkingDone = false;
                                this.scrollToBottom();

                            } else if (e.type === 'thinking') {
                                const arr = this.messages[bi].thinkings;
                                if (arr.length > 0 && !arr[arr.length - 1].done) {
                                    arr[arr.length - 1].text += e.data.text;
                                }
                                this.scrollToBottom();

                            } else if (e.type === 'thinking_end') {
                                const arr = this.messages[bi].thinkings;
                                if (arr.length > 0) {
                                    const cur = arr[arr.length - 1];
                                    cur.done = true;
                                    if (cur.durationTimer) {
                                        clearInterval(cur.durationTimer);
                                        cur.durationTimer = null;
                                    }
                                    cur.duration = Math.max(1, Math.round((Date.now() - cur.startTime) / 1000));
                                    cur.collapsed = true;
                                }
                                stopThinkingTimer();
                                this.scrollToBottom();

                            } else if (e.type === 'token') {
                                pendingTokenText += e.data.text;
                                scheduleTokenFlush();

                            } else if (e.type === 'tool_start') {
                                const step = {
                                    label: e.data.label || e.data.tool,
                                    icon: e.data.icon || '🔧',
                                    description: e.data.description || '',
                                    argsSummary: e.data.args_summary || '',
                                    resultPreview: '',
                                    status: 'running',
                                    round: e.data.round,
                                    indexInBatch: e.data.index_in_batch,
                                    batchSize: e.data.batch_size,
                                };
                                if (!this.messages[bi].workflowSteps) this.messages[bi].workflowSteps = [];
                                this.messages[bi].workflowSteps.push(step);

                                const groups = this.messages[bi].traceGroups || [];
                                const g = groups[groups.length - 1];
                                if (g && g.round === e.data.round) {
                                    g.tools.push({
                                        name: e.data.tool,
                                        label: e.data.label || e.data.tool,
                                        icon: e.data.icon || '🔧',
                                        argsSummary: e.data.args_summary || '',
                                        description: e.data.description || '',
                                        status: 'running',
                                        durationMs: 0,
                                        resultPreview: '',
                                    });
                                }
                                this.messages[bi].toolStatus = '';
                                this.scrollToBottom();

                            } else if (e.type === 'tool_end') {
                                const steps = this.messages[bi].workflowSteps;
                                if (steps && steps.length > 0) {
                                    const last = steps[steps.length - 1];
                                    last.status = 'done';
                                    last.durationMs = e.data.duration_ms;
                                    const rp = e.data.result_preview || '';
                                    last.resultPreview = rp ? rp.replace(/[\r\n]+/g, ' ').slice(0, 120) + (rp.length > 120 ? '...' : '') : '执行完成';
                                }
                                const groups = this.messages[bi].traceGroups || [];
                                const g = groups[groups.length - 1];
                                if (g) {
                                    const t = g.tools[g.tools.length - 1];
                                    if (t) {
                                        t.status = 'done';
                                        t.durationMs = e.data.duration_ms || 0;
                                        const rp = e.data.result_preview || '';
                                        t.resultPreview = rp ? rp.replace(/[\r\n]+/g, ' ').slice(0, 150) + (rp.length > 150 ? '...' : '') : '';
                                    }
                                }
                                // 页面工具改动了工作区 pages/ → 面板开着就同步刷新，
                                // 没开着则提示一句，省得用户不知道页面已经做好了
                                if (e.data.tool === 'create_page' || e.data.tool === 'edit_page') {
                                    if (this.rightPanelOpen && this.rightPanelTab === 'pages') {
                                        this.loadPages();
                                    } else if (e.data.tool === 'create_page') {
                                        this.showToast('页面已生成，可点顶部「页面预览」查看', 'success');
                                    }
                                }
                                this.scrollToBottom();

                            } else if (e.type === 'summary') {
                                this.messages[bi].traceTotalMs = e.data.total_ms || 0;
                                this.messages[bi].traceRounds = e.data.rounds || 0;
                                this.messages[bi].traceToolTimeMs = e.data.total_tool_time_ms || 0;
                                this.messages[bi].traceLlmTimeMs = e.data.total_llm_time_ms || 0;
                                this.messages[bi].traceToolCalls = e.data.tool_calls || [];
                                this.scrollToBottom();

                            } else if (e.type === 'plan_result') {
                                const p = e.data || {};
                                const planSteps = (p.plan || []).map((s, i) => `  ${i + 1}. ${s}`).join('\n');
                                const criteria = (p.success_criteria || []).map(s => `  ✓ ${s}`).join('\n');
                                const desc = `🎯 ${p.goal || '(未明确)'}\n${planSteps ? '📋 执行步骤:\n' + planSteps : ''}${criteria ? '\n✅ 成功标准:\n' + criteria : ''}`;
                                if (!this.messages[bi].workflowSteps) this.messages[bi].workflowSteps = [];
                                this.messages[bi].workflowSteps.push({
                                    label: `📋 执行计划（第 ${p.attempt || 1} 轮）`,
                                    icon: '📋',
                                    description: desc,
                                    argsSummary: p.judge_feedback_applied ? '🔁 已结合上一轮 Judge 反馈修订' : '',
                                    resultPreview: '',
                                    status: 'done',
                                });
                                this.scrollToBottom();

                            } else if (e.type === 'judge_result') {
                                const j = e.data || {};
                                const strengths = (j.strengths || []).map(s => `  ✅ ${s}`).join('\n');
                                const issues = (j.issues || []).map(s => `  ❌ ${s}`).join('\n');
                                const passedIcon = j.passed ? '✅' : '❌';
                                const status = j.passed ? 'done' : 'warning';
                                const desc = `${passedIcon} Judge 打分: **${j.score}**（及格线 ${document._jps || 60}）\n`
                                           + (strengths ? strengths + '\n' : '')
                                           + (issues ? issues : '');
                                if (!this.messages[bi].workflowSteps) this.messages[bi].workflowSteps = [];
                                this.messages[bi].workflowSteps.push({
                                    label: `${passedIcon} Judge 评审（第 ${j.attempt || 1} 轮）: ${j.score} 分 ${j.passed ? '通过' : '未通过'}`,
                                    icon: j.passed ? '✅' : '❌',
                                    description: desc,
                                    argsSummary: `耗时 ${j.duration_ms || 0}ms · ${j.judge_model || ''}`,
                                    resultPreview: j.feedback_for_replan ? `给 Planner 的反馈: ${j.feedback_for_replan}` : '',
                                    status: status,
                                });
                                this.scrollToBottom();

                            } else if (e.type === 'attachments') {
                                fa = e.data;

                            } else if (e.type === 'done') {
                                gotDone = true;
                                stopThinkingTimer();
                                if (pendingTokenText) {
                                    ft += pendingTokenText;
                                    pendingTokenText = '';
                                }
                                this.messages[bi].streaming = false;
                                this.messages[bi].toolStatus = '';
                                finalReply = e.data.reply_text || ft;
                                this.messages[bi].text = finalReply;
                                if (e.data.total_ms && !this.messages[bi].traceTotalMs) {
                                    this.messages[bi].traceTotalMs = e.data.total_ms;
                                }
                                if (e.data.session_id) this.sessionId = e.data.session_id;
                                if (fa) this.messages[bi].attachments = fa;
                                this.saveCurrentSession(t || (hasFiles ? '(上传附件)' : '(空)'), finalReply, fa, userAtts, botMsg, userPicks);

                            } else if (e.type === 'error') {
                                gotDone = true;
                                stopThinkingTimer();
                                this.messages[bi].text = '❌ ' + e.data.message;
                                this.messages[bi].streaming = false;
                                this.messages[bi].toolStatus = '';
                            }
                        } catch (e) {
                            console.warn('SSE err:', e, d);
                        }
                    }
                }

                // 流结束但没收到 done/error：说明连接被中断，
                // 之前会留下永远转圈的"生成中"气泡，必须显式收尾。
                if (!gotDone && !aborted) {
                    this.messages[bi].streaming = false;
                    this.messages[bi].toolStatus = '';
                    if (!this.messages[bi].text) {
                        this.messages[bi].text = '⚠️ 连接已中断，未能收到完整回复。请重试。';
                    } else {
                        this.messages[bi].text += '\n\n> ⚠️ 连接中断，以上内容可能不完整。';
                    }
                    if (finalReply) {
                        this.saveCurrentSession(t || (hasFiles ? '(上传附件)' : '(空)'), finalReply, fa, userAtts, botMsg, userPicks);
                    }
                }
                if (aborted) {
                    this.messages[bi].streaming = false;
                    this.messages[bi].toolStatus = '';
                    this.messages[bi].text = (this.messages[bi].text || '') +
                        '\n\n> ⏱️ 响应超时（超过 ' + Math.round(STREAM_IDLE_TIMEOUT / 1000) + ' 秒无数据），已中止。';
                }
            } catch (e) {
                const isAbort = e.name === 'AbortError' || aborted;
                const msg = botMsg || this.messages[bi];
                if (isAbort) {
                    if (pendingTokenText) {
                        ft += pendingTokenText;
                        msg.text = ft;
                        pendingTokenText = '';
                    } else if (ft && !msg.text) {
                        msg.text = ft;
                    }
                    msg.streaming = false;
                    msg.toolStatus = '';
                    if (msg.text) {
                        msg.text += '\n\n> ⏹️ 已中断生成';
                    } else {
                        msg.text = '⏹️ 已中断生成';
                    }
                } else {
                    console.error(e);
                    msg.text = '❌ 请求失败：' + e.message;
                    msg.streaming = false;
                    msg.toolStatus = '';
                }
            } finally {
                // 【顺序很重要】先把界面状态清掉，再做冲刷/高亮/滚动这些收尾。
                // 之前把它们混在一起，收尾里任何一步抛错都会让 isLoading 永远为 true，
                // 界面永久停在"正在生成回复"（实测踩过：finally 首行 ReferenceError）。
                this.isLoading = false;
                this.loadingLabel = '';
                this._abortController = null;
                try {
                    if (pendingTokenText) {
                        ft += pendingTokenText;
                        this.messages[bi].text = ft;
                        pendingTokenText = '';
                    }
                    stopThinkingTimer();
                    if (idleTimer) clearTimeout(idleTimer);
                    this.highlightCodeBlocks();
                    this.scrollToBottom(true);
                } catch (e) {
                    // 收尾失败只记日志：界面状态已经清干净，不影响用户继续操作
                    console.error('流收尾失败（界面状态已复位）:', e);
                }
            }
        },

        stopGeneration() {
            if (this._abortController) {
                this._abortController.abort();
                this._abortController = null;
            }
        },

        saveCurrentSession(ut, bt, at, uat, botMsg, upicks) {
            if (!this.sessionId) return;
            let s = this.sessions.find(x => x.id === this.sessionId);
            if (!s) {
                s = {
                    id: this.sessionId,
                    title: ut.length > 20 ? ut.slice(0, 20) + '...' : ut,
                    messages: [],
                    createdAt: Date.now(),
                    // 对话归属的工作区作用域（相对项目根的路径，'' 表示整个工作区）
                    scope: this.currentScope || '',
                    pinned: false
                };
                this.sessions.push(s);
            } else if (s.messages.length === 0) {
                s.title = ut.length > 20 ? ut.slice(0, 20) + '...' : ut;
            }
            s.messages.push({
                isUser: true,
                text: ut,
                time: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
                userAtts: uat && uat.length > 0 ? uat : null,
                userPicks: upicks && upicks.length > 0 ? upicks : null
            });
            // 注意：botMsg 必须由调用方传入。
            // 早期版本这里引用了 sendMessage 的局部变量 bi，导致每次回复都抛
            // ReferenceError 并被 catch 静默吞掉 —— 会话历史永远写不进 localStorage，
            // 刷新页面后侧边栏和对话全部丢失。
            s.messages.push({
                isUser: false,
                text: bt,
                time: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
                attachments: at || undefined,
                thinking: (botMsg && botMsg.thinking) || undefined,
                thinkingDone: (botMsg && botMsg.thinkingDone) || false,
                thinkingDuration: (botMsg && botMsg.thinkingDuration != null) ? botMsg.thinkingDuration : undefined,
                thinkingCollapsed: true
            });
            this.saveSessions();
        },

        toggleMenu(sid, event) {
            if (this.menuSessionId === sid) {
                this.closeMenu();
                return;
            }
            const rect = event.currentTarget.getBoundingClientRect();
            this.menuSessionId = sid;
            this.menuPos = { left: rect.right + 6 + 'px', top: rect.top + 'px' };
        },

        closeMenu() {
            this.menuSessionId = null;
            this.menuPos = {};
        },

        toggleSidebar() {
            this.sidebarCollapsed = !this.sidebarCollapsed;
        },

        /**
         * 顶部导航左侧按钮：开合左侧边栏。
         * 窄屏的侧边栏是抽屉（看 sidebarOpen），宽屏是收窄到 0（看 sidebarCollapsed）。
         */
        toggleLeftSidebar() {
            if (this.isNarrow) {
                this.sidebarOpen = !this.sidebarOpen;
                return;
            }
            this.toggleSidebar();
        },

        /** 顶部导航右侧按钮：开合右侧栏（页面预览 / 查看工作区文件的入口在其顶部） */
        toggleRightSidebar() {
            this.rightPanelOpen = !this.rightPanelOpen;
            if (this.rightPanelOpen) this.refreshRightPanel();
        },

        getPinned(sid) {
            const s = this.sessions.find(x => x.id === sid);
            return s ? s.pinned : false;
        },

        togglePin(sid) {
            const s = this.sessions.find(x => x.id === sid);
            if (s) {
                s.pinned = !s.pinned;
                this.saveSessions();
            }
            this.closeMenu();
        },

        renameSession(sid) {
            const s = this.sessions.find(x => x.id === sid);
            if (!s) return;
            this.renamingId = sid;
            this.renameValue = s.title || '';
            this.closeMenu();
            this.$nextTick(() => {
                const el = this.$refs.renameInput;
                if (el) {
                    el.focus();
                    el.select();
                }
            });
        },

        cancelRename() {
            this.renamingId = null;
            this.renameValue = '';
        },

        confirmRename() {
            const s = this.sessions.find(x => x.id === this.renamingId);
            if (s && this.renameValue.trim()) {
                s.title = this.renameValue.trim();
                this.saveSessions();
            }
            this.cancelRename();
        },

        shareSession(sid) {
            const s = this.sessions.find(x => x.id === sid);
            if (!s) return;
            const txt = `对话: ${s.title || '新对话'}`;
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(txt)
                    .then(() => this.showToast('已复制标题到剪贴板', 'success'))
                    // 非安全源（http 局域网访问）或权限被拒时 writeText 会 reject，
                    // 没有 catch 会产生 unhandled rejection，且界面此前会误报成功
                    .catch(() => this.showToast('复制失败，请手动复制: ' + txt, 'info'));
            } else {
                this.showToast('分享: ' + txt, 'info');
            }
            this.closeMenu();
        },

        newChat() {
            // 生成中不允许切换：流式回调按消息下标写数据，切换后会写进错误的会话
            if (this.isLoading) {
                this.showToast('正在生成回复，请稍候再新建对话', 'info');
                return;
            }
            this.messages = [];
            this.sessionId = null;
            this.userInput = '';
            this.fileContents = [];
            this.sidebarOpen = false;
            this.currentView = 'chat';
            // 必须清掉，否则刷新后 created() 会把上一个会话又恢复出来
            localStorage.removeItem(this.sessionsKey('lastSessionId'));
            this.autoResize();
        },

        switchSession(sid) {
            if (this.isLoading) {
                this.showToast('正在生成回复，请稍候再切换会话', 'info');
                return;
            }
            const s = this.sessions.find(x => x.id === sid);
            if (s) {
                this.sessionId = sid;
                this.messages = s.messages.map(m => ({ ...m }));
                // 恢复该对话所属的工作区作用域
                this.currentScope = s.scope || '';
                this.userInput = '';
                this.fileContents = [];
                this.sidebarOpen = false;
                this.currentView = 'chat';
                localStorage.setItem(this.sessionsKey('lastSessionId'), sid);
                this.$nextTick(() => {
                    this.scrollToBottom();
                    this.highlightCodeBlocks();
                });
            }
        },

        confirmDeleteSession(sid) {
            this.closeMenu();
            this.confirmTitle = '删除对话';
            this.confirmDesc = '删除后将无法恢复该对话记录。';
            this.confirmBtnText = '删除';
            this.confirmCallback = () => {
                this.sessions = this.sessions.filter(s => s.id !== sid);
                this.saveSessions();
                if (this.sessionId === sid) {
                    localStorage.removeItem(this.sessionsKey('lastSessionId'));
                    this.newChat();
                }
            };
            this.showConfirm = true;
        },

        clearChat() {
            if (this.messages.length === 0) {
                this.newChat();
                return;
            }
            this.confirmTitle = '清空对话';
            this.confirmDesc = '当前对话的所有消息将被清除。';
            this.confirmBtnText = '清空';
            this.confirmCallback = () => {
                if (this.sessionId) {
                    fetch(API + '/sessions/' + this.sessionId + '/clear', { method: 'POST' }).catch(() => {});
                    this.sessions = this.sessions.filter(s => s.id !== this.sessionId);
                    this.saveSessions();
                }
                localStorage.removeItem(this.sessionsKey('lastSessionId'));
                this.newChat();
            };
            this.showConfirm = true;
        },

        confirmAction() {
            this.showConfirm = false;
            if (this.confirmCallback) {
                this.confirmCallback();
                this.confirmCallback = null;
            }
        },

        cancelModal() {
            this.showConfirm = false;
            this.confirmCallback = null;
        },

        previewImage(u) {
            this.previewImg = u;
        },

        copyMessage(msg) {
            const t = msg.text || '';
            const markCopied = () => {
                msg._copied = true;
                setTimeout(() => { msg._copied = false; }, 2000);
            };
            const fallback = () => {
                try {
                    const ta = document.createElement('textarea');
                    ta.value = t;
                    ta.style.position = 'fixed';
                    ta.style.opacity = '0';
                    document.body.appendChild(ta);
                    ta.select();
                    document.execCommand('copy');
                    document.body.removeChild(ta);
                    markCopied();
                } catch (e) {
                    this.showToast('复制失败，请手动选择文本复制', 'info');
                }
            };
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(t).then(markCopied).catch(fallback);
            } else {
                fallback();
            }
        },

        showToast(m, type = 'info') {
            let c = document.getElementById('toast-container');
            if (!c) {
                c = document.createElement('div');
                c.id = 'toast-container';
                c.className = 'toast-container';
                document.body.appendChild(c);
            }
            const el = document.createElement('div');
            el.className = 'toast toast-' + type;
            el.textContent = m;
            c.appendChild(el);
            setTimeout(() => {
                el.style.opacity = '0';
                el.style.transform = 'translateX(20px)';
                el.style.transition = 'all .3s';
                setTimeout(() => el.remove(), 300);
            }, 2500);
        },

        quickPrompt(t) {
            this.userInput = t;
            this.sendMessage();
        },

        scrollToBottom(force = false) {
            this.$nextTick(() => {
                const el = this.$refs.messagesContainer;
                if (!el) return;
                if (force) {
                    el.scrollTop = el.scrollHeight;
                    return;
                }
                const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
                if (distance < 150) {
                    el.scrollTop = el.scrollHeight;
                }
            });
        },

        // ── 工作区文件树 ──────────────────────────────────────────────────
        /** 在已加载的节点里按路径查找（当前最多两层，够用且便宜） */
        findNode(path) {
            if (!path) return null;
            for (const top of (this.treeStore[''] || [])) {
                if (top.path === path) return top;
                const kids = this.treeStore[top.path];
                if (kids) {
                    for (const k of kids) if (k.path === path) return k;
                }
            }
            return null;
        },

        async loadTree(path = '') {
            if (this.treeLoading[path]) return;
            this.treeLoading = { ...this.treeLoading, [path]: true };
            try {
                const q = path ? '?dir=' + encodeURIComponent(path) : '';
                const r = await fetch(API + '/workspace/tree' + q);
                if (!r.ok) throw new Error('HTTP ' + r.status);
                const d = await r.json();
                const nodes = (d.entries || []).map(e => ({ ...e }));
                this.treeStore[path] = nodes;
                if (!path) {
                    this.workspace = {
                        name: d.root_name || '工作区',
                        path: d.root || '',
                        count: nodes.length,
                        truncated: !!d.truncated,
                    };
                }
                this.treeVersion++;
                return nodes;
            } catch (e) {
                console.warn('工作区加载失败:', e);
                if (!path) this.showToast('无法读取工作区目录：' + e.message, 'error');
                return [];
            } finally {
                const t = { ...this.treeLoading };
                delete t[path];
                this.treeLoading = t;
            }
        },

        isExpanded(path) {
            void this.treeVersion;
            return !!this.expandedPaths[path];
        },

        childrenOf(path) {
            void this.treeVersion;
            return this.treeStore[path] || [];
        },

        async toggleDir(node) {
            const p = node.path;
            if (this.expandedPaths[p]) {
                const e = { ...this.expandedPaths };
                delete e[p];
                this.expandedPaths = e;
                return;
            }
            this.expandedPaths = { ...this.expandedPaths, [p]: true };
            if (!this.treeStore[p]) await this.loadTree(p);
        },

        /** 在当前作用域下开始一个新对话（不落盘，等第一条消息再创建） */
        startNewChatInScope() {
            this.messages = [];
            this.sessionId = null;
            this.userInput = '';
            this.fileContents = [];
            this.currentView = 'chat';
            localStorage.removeItem(this.sessionsKey('lastSessionId'));
            this.autoResize();
        },

        // ── 工作区管理 ────────────────────────────────────────────────────
        async loadWorkspaces() {
            try {
                const r = await fetch(API + '/workspaces');
                if (!r.ok) throw new Error('HTTP ' + r.status);
                const d = await r.json();
                this.workspaces = {
                    list: d.workspaces || [],
                    active_id: d.active_id || null,
                    active: d.active || null,
                    base_dir: d.base_dir || '',
                };
                this.loadAllWorkspaceSessions();
            } catch (e) {
                console.warn('工作区列表加载失败:', e);
            }
        },

        /**
         * 读取**每个**工作区的对话记录。
         *
         * 侧边栏要同时展示所有工作区及其对话，所以不能只读当前激活的那个。
         */
        loadAllWorkspaceSessions() {
            const map = {};
            for (const w of this.workspaces.list) {
                map[w.id] = this.readSessionsFor(w.id);
            }
            this.wsSessions = map;
            this._wsSessionsVersion = (this._wsSessionsVersion || 0) + 1;
        },

        /** 读取某个工作区的对话（纯读，不碰 this.sessions） */
        readSessionsFor(wsId) {
            try {
                const d = localStorage.getItem('chatSessions::' + wsId);
                if (!d) return [];
                return JSON.parse(d).map(s => ({
                    id: s.id,
                    title: s.title || '未命名',
                    messages: s.messages || [],
                    createdAt: s.createdAt || Date.now(),
                    updatedAt: s.updatedAt || s.createdAt || Date.now(),
                    scope: s.scope || '',
                    pinned: s.pinned || false,
                    archived: s.archived || false,
                }));
            } catch (e) {
                return [];
            }
        },

        /** 当前激活工作区的会话列表（编辑操作直接作用于它） */
        currentSessions() {
            const id = this.workspaces.active_id || 'app';
            return this.wsSessions[id] || this.sessions || [];
        },

        toggleWorkspace(id) {
            this.wsCollapsed = { ...this.wsCollapsed, [id]: !this.wsCollapsed[id] };
        },

        /** 「显示更多」：每次多放 COLLAPSED_SESSION_LIMIT 条 */
        revealMore(wsId) {
            const cur = this.wsReveal[wsId] || COLLAPSED_SESSION_LIMIT;
            this.wsReveal = { ...this.wsReveal, [wsId]: cur + COLLAPSED_SESSION_LIMIT };
        },

        /** 「收起」：回到默认的 5 条 */
        revealLess(wsId) {
            this.wsReveal = { ...this.wsReveal, [wsId]: COLLAPSED_SESSION_LIMIT };
        },

        /** 手动排序：按 localStorage 里保存的会话顺序 */
        sortByManual(wsId, list) {
            let order = [];
            try {
                order = JSON.parse(localStorage.getItem('sessionOrder::' + wsId) || '[]');
            } catch (e) { order = []; }
            if (!order.length) return list;
            const idx = new Map(order.map((id, i) => [id, i]));
            return list.slice().sort((a, b) => {
                const ia = idx.has(a.id) ? idx.get(a.id) : Number.MAX_SAFE_INTEGER;
                const ib = idx.has(b.id) ? idx.get(b.id) : Number.MAX_SAFE_INTEGER;
                if (ia !== ib) return ia - ib;
                return (b.updatedAt || b.createdAt || 0) - (a.updatedAt || a.createdAt || 0);
            });
        },

        openViewMenu(ev) {
            ev.stopPropagation();
            if (this.wsViewMenuOpen) { this.wsViewMenuOpen = false; return; }
            const r = ev.currentTarget.getBoundingClientRect();
            this.wsViewMenuPos = { left: Math.max(8, r.right - 200) + 'px', top: (r.bottom + 6) + 'px' };
            this.wsViewMenuOpen = true;
            this.wsMenuFor = null;
        },

        /** 归档 / 取消归档一个会话 */
        toggleArchive(sess) {
            const wsId = this.workspaces.active_id || 'app';
            const list = this.readSessionsFor(wsId);
            const t = list.find(s => s.id === sess.id);
            if (!t) return;
            t.archived = !t.archived;
            try {
                localStorage.setItem('chatSessions::' + wsId, JSON.stringify(list));
            } catch (e) {}
            this.loadSessions();
            this.loadAllWorkspaceSessions();
            this.showToast(t.archived ? '已归档' : '已取消归档', 'success');
        },

        /** 相对时间：13分钟 / 12天 */
        relTime(ts) {
            if (!ts) return '';
            const diff = Date.now() - ts;
            const m = Math.floor(diff / 60000);
            if (m < 1) return '刚刚';
            if (m < 60) return m + '分钟';
            const h = Math.floor(m / 60);
            if (h < 24) return h + '小时';
            const d = Math.floor(h / 24);
            if (d < 30) return d + '天';
            const mo = Math.floor(d / 30);
            if (mo < 12) return mo + '个月';
            return Math.floor(mo / 12) + '年';
        },

        openWsMenu(id, ev) {
            ev.stopPropagation();
            if (this.wsMenuFor === id) { this.wsMenuFor = null; return; }
            const rect = ev.currentTarget.getBoundingClientRect();
            this.wsMenuPos = { left: rect.right + 6 + 'px', top: rect.top + 'px' };
            this.wsMenuFor = id;
        },

        closeWsMenu() { this.wsMenuFor = null; },

        startWsRename(w) {
            this.wsMenuFor = null;
            this.wsRenamingId = w.id;
            this.wsRenameValue = w.name;
            this.$nextTick(() => {
                const el = this.$refs.wsRenameInput;
                if (el && el.focus) el.focus();
            });
        },

        cancelWsRename() {
            this.wsRenamingId = null;
            this.wsRenameValue = '';
        },

        async confirmWsRename() {
            const id = this.wsRenamingId;
            const name = (this.wsRenameValue || '').trim();
            if (!id) return;
            if (!name) { this.cancelWsRename(); return; }
            try {
                const r = await fetch(API + '/workspaces/' + id, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name }),
                });
                const d = await r.json();
                if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
                await this.loadWorkspaces();
                this.showToast('已重命名为：' + name, 'success');
            } catch (e) {
                this.showToast('重命名失败：' + e.message, 'error');
            } finally {
                this.cancelWsRename();
            }
        },

        async removeWorkspace(w) {
            this.wsMenuFor = null;
            this.confirmTitle = '移除工作区';
            this.confirmDesc = `将从列表中移除「${w.name}」。\n\n磁盘上的文件夹不会被删除，只移除这条登记。`;
            this.confirmBtnText = '移除';
            this.confirmCallback = async () => {
                try {
                    const r = await fetch(API + '/workspaces/' + w.id, { method: 'DELETE' });
                    const d = await r.json();
                    if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
                    await this.loadWorkspaces();
                    this.applyWorkspaceSessions();
                    if (this.workspaces.active_id) {
                        this.treeStore = { '': [] };
                        this.expandedPaths = {};
                        this.treeVersion++;
                        this.currentScope = '';
                        this.messages = [];
                        this.sessionId = null;
                        await this.loadTree('');
                    }
                    this.showToast('已移除工作区：' + w.name, 'success');
                } catch (e) {
                    this.showToast('移除失败：' + e.message, 'error');
                }
            };
            this.showConfirm = true;
        },

        /** 在当前激活的工作区里新建对话 */
        newChatInWorkspace(w) {
            if (this.isLoading) {
                this.showToast('正在生成回复，请稍候', 'info');
                return;
            }
            if (w && w.id !== this.workspaces.active_id) {
                this.activateWorkspace({ id: w.id, name: w.name });
                return;
            }
            this.newChat();
        },

        async activateWorkspace(w) {
            if (this.isLoading) {
                this.showToast('正在生成回复，请稍候再切换工作区', 'info');
                return;
            }
            this.wsMenuOpen = false;
            try {
                const r = await fetch(API + '/workspaces/' + w.id + '/activate', { method: 'POST' });
                const d = await r.json();
                if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
                await this.loadWorkspaces();
                // 切换工作区 = 换一棵树 + 换一套对话记录
                this.treeStore = { '': [] };
                this.expandedPaths = {};
                this.treeVersion++;
                this.currentScope = '';
                this.messages = [];
                this.sessionId = null;
                await this.loadTree('');
                this.applyWorkspaceSessions();
                // 页面与文件都属于工作区：面板开着就换成新工作区的内容
                if (this.rightPanelOpen) {
                    if (this.rightPanelTab === 'pages') await this.loadPages();
                    // 文件树刚被 loadTree('') 刷新过，但展开态与已选文件要清掉
                    this.expandedPaths = {};
                    this.selectedFilePath = '';
                    this.filePreview = null;
                    this.filePreviewError = '';
                }
                this.showToast('已切换到工作区：' + w.name, 'success');
            } catch (e) {
                this.showToast('切换失败：' + e.message, 'error');
            }
        },

        // ── 新建工作区：浏览本机文件夹 ─────────────────────────────────────
        openWorkspacePicker() {
            this.wsMenuOpen = false;
            this.wsPickerOpen = true;
            this.wsPickMode = 'folder';
            this.wsSelectedPath = '';
            this.wsSelectedFiles = [];
            this.wsNewName = '';
            this.wsNewFolderName = '';
            this.wsPathInput = '';
            this.wsLoad('');
        },

        closeWorkspacePicker() {
            this.wsPickerOpen = false;
        },

        async wsLoad(path) {
            this.wsLoading = true;
            try {
                const params = new URLSearchParams();
                params.set('path', path || '');
                if (this.wsPickMode === 'files') {
                    params.set('include_files', 'true');
                    params.set('file_select', 'true');
                }
                const r = await fetch(API + '/workspaces/browse?' + params.toString());
                const d = await r.json();
                if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
                this.wsBrowseData = d;
                this.wsPathInput = d.path || '';
                if (this.wsPickMode === 'files') {
                    if (!d.is_root) this.wsSelectedPath = d.path;
                    else this.wsSelectedPath = '';
                } else {
                    if (!d.is_root) this.wsSelectedPath = d.path;
                    else this.wsSelectedPath = '';
                }
            } catch (e) {
                this.showToast('无法打开：' + e.message, 'error');
            } finally {
                this.wsLoading = false;
            }
        },

        wsTogglePickMode(mode) {
            if (this.wsPickMode === mode) return;
            this.wsPickMode = mode;
            this.wsSelectedFiles = [];
            this.wsLoad(this.wsBrowseData.path || '');
        },

        wsToggleFile(path) {
            const idx = this.wsSelectedFiles.indexOf(path);
            if (idx >= 0) this.wsSelectedFiles.splice(idx, 1);
            else this.wsSelectedFiles.push(path);
        },

        wsFileSelected(path) {
            return this.wsSelectedFiles.indexOf(path) >= 0;
        },

        wsBrowseParent() {
            const p = this.wsBrowseData.parent;
            if (p) this.wsLoad(p);
            else this.wsLoad('');
        },

        async wsCreateFolder() {
            const name = (this.wsNewFolderName || '').trim();
            if (!name) return;
            try {
                const r = await fetch(API + '/workspaces/mkdir', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ parent: this.wsBrowseData.path, name }),
                });
                const d = await r.json();
                if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
                this.wsNewFolderName = '';
                await this.wsLoad(this.wsBrowseData.path);
                this.wsSelectedPath = d.path;
                this.showToast('已创建文件夹：' + name, 'success');
            } catch (e) {
                this.showToast('创建失败：' + e.message, 'error');
            }
        },

        async confirmCreateWorkspace() {
            if (this.wsCreating) return;
            if (this.wsPickMode === 'files') {
                if (!this.wsSelectedFiles.length) {
                    this.showToast('请至少选择一个文件', 'error');
                    return;
                }
            } else {
                if (!this.wsSelectedPath) return;
            }
            this.wsCreating = true;
            try {
                let body;
                if (this.wsPickMode === 'files') {
                    body = {
                        kind: 'files',
                        files: this.wsSelectedFiles.slice(),
                        name: this.wsNewName || '',
                    };
                } else {
                    body = { path: this.wsSelectedPath, name: this.wsNewName };
                }
                const r = await fetch(API + '/workspaces', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                const d = await r.json();
                if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
                this.wsPickerOpen = false;
                await this.loadWorkspaces();
                this.treeStore = { '': [] };
                this.expandedPaths = {};
                this.treeVersion++;
                this.currentScope = '';
                this.messages = [];
                this.sessionId = null;
                await this.loadTree('');
                this.applyWorkspaceSessions();
                const label = this.wsPickMode === 'files'
                    ? `工作区已创建（${this.wsSelectedFiles.length} 个文件）`
                    : ('工作区已创建：' + d.workspace.name);
                this.showToast(label, 'success');
            } catch (e) {
                this.showToast('创建失败：' + e.message, 'error');
            } finally {
                this.wsCreating = false;
            }
        },

        /** 切换工作区后重新挂载该工作区的对话记录 */
        applyWorkspaceSessions() {
            this.sessions = [];
            this.loadSessions();
        },

        // ── 设置面板 ──────────────────────────────────────────────────────
        openSettings() {
            this.settingsOpen = true;
            this.sidebarOpen = false;
            this.loadRuntimeInfo();
        },

        closeSettings() {
            this.settingsOpen = false;
        },

        notImplemented(name) {
            this.showToast(`「${name}」暂未实现：当前配置通过项目根目录的 .env 文件维护`, 'info');
        },

        /** 读取后端只读运行时信息，用于「模型」标签页展示 */
        async loadRuntimeInfo() {
            try {
                const r = await fetch(API + '/kb/chroma/list');
                if (r.ok) {
                    const d = await r.json();
                    const files = d.files || [];
                    this.runtimeInfo.kbDocs = files.length;
                    this.runtimeInfo.kbChunks = files.reduce((a, f) => a + (f.chunks || 0), 0);
                }
            } catch (e) { /* 后端未启动时静默降级 */ }

            try {
                const r = await fetch(API + '/runtime/info');
                if (r.ok) {
                    const d = await r.json();
                    this.runtimeInfo.model = d.model || '';
                    this.runtimeInfo.temperature = d.temperature ?? '';
                    this.runtimeInfo.embedding = d.embedding || '';
                    this.runtimeInfo.tools = d.tools ?? null;
                    this.runtimeInfo.base_url = d.base_url || '';
                    this.runtimeInfo.api_key_masked = d.api_key_masked || '';
                    this.runtimeInfo.api_key_set = !!d.api_key_set;
                    this.runtimeInfo.env_file = d.env_file || '';
                    if (d.writable !== undefined) this.runtimeInfo.writable = !!d.writable;
                    this.resetApiForm();
                    if (!this.runtimeInfo.writable) {
                        this.apiSaveHint = '后端未允许从当前来源修改（生产模式下仅限本机访问）';
                        this.apiHintClass = 'settings-hint-bad';
                    }
                }
            } catch (e) { /* 旧版后端没有该接口，忽略 */ }
        },

        /** 把表单恢复成"当前后端生效的值"（密钥永远留空 = 不修改） */
        resetApiForm() {
            this.apiForm.base_url = this.runtimeInfo.base_url || '';
            this.apiForm.api_key = '';
            this.apiKeyVisible = false;
            this.apiHintClass = '';
            this.apiSaveHint = '保存前会先做一次真实连通测试，通过才生效';
        },

        /**
         * 保存并立即生效（免重启）。
         *
         * 后端顺序是"先验证后落盘"：测试不过就整单拒绝、原配置不变，
         * 所以这里只需把错误原样展示给用户，不用做回滚。
         */
        async saveApiSettings() {
            if (this.apiSaving) return;
            const baseUrl = (this.apiForm.base_url || '').trim();
            const apiKey = (this.apiForm.api_key || '').trim();
            if (!baseUrl && !apiKey) {
                this.apiHintClass = 'settings-hint-bad';
                this.apiSaveHint = '没有需要修改的内容：两项都留空';
                return;
            }
            this.apiSaving = true;
            this.apiHintClass = '';
            this.apiSaveHint = '正在验证新配置…';
            try {
                const r = await fetch(API + '/settings/api', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ api_key: apiKey, base_url: baseUrl, verify: true }),
                });
                const d = await r.json().catch(() => ({}));
                if (!r.ok) {
                    const msg = (d && d.detail) ? d.detail : ('HTTP ' + r.status);
                    this.apiHintClass = 'settings-hint-bad';
                    this.apiSaveHint = msg;
                    this.showToast('保存失败：' + msg, 'error');
                    return;
                }

                this.runtimeInfo.base_url = d.base_url || '';
                this.runtimeInfo.api_key_masked = d.api_key_masked || '';
                this.runtimeInfo.api_key_set = !!d.api_key_set;
                const changed = Array.isArray(d.changed) ? d.changed : [];
                this.resetApiForm();
                const ms = d.verify && d.verify.ms ? ('连通测试 ' + d.verify.ms + 'ms') : '已验证';
                if (!changed.length) {
                    this.apiSaveHint = '配置没有变化，当前已是这个值';
                    this.showToast('配置没有变化', 'info');
                } else if (d.persist_error) {
                    this.apiHintClass = 'settings-hint-bad';
                    this.apiSaveHint = '已生效，但写入 .env 失败（重启后会变回旧值）：' + d.persist_error;
                    this.showToast('已生效，但持久化失败：' + d.persist_error, 'error');
                } else {
                    this.apiHintClass = 'settings-hint-ok';
                    this.apiSaveHint = '已生效并写入 .env（无需重启）｜' + ms;
                    this.showToast('API 已切换并生效（无需重启）', 'success');
                }
            } catch (e) {
                this.apiHintClass = 'settings-hint-bad';
                this.apiSaveHint = '请求失败：' + (e && e.message ? e.message : e);
                this.showToast('保存失败：' + (e && e.message ? e.message : e), 'error');
            } finally {
                this.apiSaving = false;
            }
        },

        setThemeMode(mode) {
            this.themeMode = mode;
            localStorage.setItem('chatThemeMode', mode);
            this.applyTheme();
        },

        loadTheme() {
            // 兼容旧的布尔值存储（早期只有浅色/深色两态）
            const legacy = localStorage.getItem('chatTheme');
            let mode = localStorage.getItem('chatThemeMode');
            if (!mode) mode = legacy === 'dark' ? 'dark' : 'light';

            this.themeMode = mode;
            this.systemPrefersDark = window.matchMedia
                ? window.matchMedia('(prefers-color-scheme: dark)').matches
                : false;
            this.applyTheme();
        },

        /** 侧边栏那个日月按钮：在浅色/深色之间切换（跟随系统时先切成显式值） */
        toggleTheme() {
            const currentlyDark = this.isDark;
            this.setThemeMode(currentlyDark ? 'light' : 'dark');
        },

        applyTheme() {
            const dark = this.themeMode === 'dark'
                || (this.themeMode === 'system' && this.systemPrefersDark);
            this.isDark = dark;
            document.documentElement.classList.toggle('dark', dark);
            // 同步旧字段，避免其他地方读到过期的值
            localStorage.setItem('chatTheme', dark ? 'dark' : 'light');

            // 让浏览器原生控件（下拉框、滚动条、数字输入）也跟随主题
            const meta = document.querySelector('meta[name="color-scheme"]');
            if (meta) meta.setAttribute('content', dark ? 'dark light' : 'light dark');
        },

        loadSettings() {
            try {
                const saved = JSON.parse(localStorage.getItem('chatSettings') || '{}');
                Object.assign(this.settings, saved || {});
            } catch (e) { /* 配置损坏时用默认值 */ }
            this.normalizeSettings();
            this.applyFontSize(this.settings.fontSize);
        },

        saveSettings() {
            try {
                localStorage.setItem('chatSettings', JSON.stringify(this.settings));
            } catch (e) { /* 隐私模式下可能写失败，忽略 */ }
        },

        /** 夹取非法输入（用户可能手打 999 或清空输入框） */
        normalizeSettings() {
            const n = Number(this.settings.fontSize);
            this.settings.fontSize = Number.isFinite(n)
                ? Math.min(22, Math.max(12, Math.round(n)))
                : 16;
        },

        applyFontSize(px) {
            const v = Math.min(22, Math.max(12, Number(px) || 16));
            document.documentElement.style.setProperty('--chat-font-size', v + 'px');
        },

        startResize(e) {
            this.resizing = true;
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';
            const startX = e.clientX;
            const startW = this.sidebarWidth;

            const onMove = (ev) => {
                const newW = Math.max(180, Math.min(500, startW + ev.clientX - startX));
                this.sidebarWidth = newW;
                document.documentElement.style.setProperty('--sidebar-w', newW + 'px');
            };
            const onUp = () => {
                this.resizing = false;
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
                localStorage.setItem('sidebarWidth', this.sidebarWidth);
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
            };
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        },

        // ── 右侧面板（页面预览 / 工作区文件）────────────────────────────────

        /** 右栏顶部的两个内容入口：页面预览 / 查看工作区文件 */
        switchRightTab(tab) {
            if (this.rightPanelTab === tab) return;
            this.rightPanelTab = tab;
            this.refreshRightPanel();
        },

        refreshRightPanel() {
            if (this.rightPanelTab === 'pages') {
                this.loadPages();
            } else {
                this.ensureTreeLoaded();
            }
        },

        /** 文件树只在首次打开时加载（之后靠「刷新」或切换工作区更新） */
        async ensureTreeLoaded() {
            if (!(this.treeStore[''] || []).length) await this.loadTree('');
        },

        async refreshTree() {
            this.treeStore = { '': [] };
            this.expandedPaths = {};
            this.treeVersion++;
            this.selectedFilePath = '';
            this.filePreview = null;
            this.filePreviewError = '';
            await this.loadTree('');
        },

        /** 点文件：只读预览文本内容（后端限制在当前工作区内，且拒绝敏感文件） */
        async openWorkspaceFile(node) {
            this.selectedFilePath = node.path;
            this.filePreviewLoading = true;
            this.filePreviewError = '';
            try {
                const r = await fetch(API + '/workspace/file?path=' + encodeURIComponent(node.path));
                const d = await r.json().catch(() => ({}));
                if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
                this.filePreview = d;
            } catch (e) {
                this.filePreview = null;
                this.filePreviewError = '无法预览：' + e.message;
            } finally {
                this.filePreviewLoading = false;
            }
        },

        /** 按扩展名给文件一个图标（认不出就用通用文件图标） */
        fileIcon(name) {
            const ext = String(name || '').toLowerCase().split('.').pop();
            const map = {
                py: 'fa-brands fa-python',
                js: 'fa-brands fa-js', mjs: 'fa-brands fa-js', ts: 'fa-brands fa-js',
                json: 'fa-solid fa-code',
                html: 'fa-brands fa-html5', htm: 'fa-brands fa-html5',
                css: 'fa-brands fa-css3-alt',
                md: 'fa-brands fa-markdown',
                csv: 'fa-solid fa-table', xlsx: 'fa-solid fa-table',
                txt: 'fa-solid fa-file-lines', log: 'fa-solid fa-file-lines',
                pdf: 'fa-solid fa-file-pdf',
                png: 'fa-solid fa-file-image', jpg: 'fa-solid fa-file-image',
                jpeg: 'fa-solid fa-file-image', gif: 'fa-solid fa-file-image',
                svg: 'fa-solid fa-file-image', webp: 'fa-solid fa-file-image',
                zip: 'fa-solid fa-file-zipper', java: 'fa-brands fa-java',
            };
            return map[ext] || 'fa-regular fa-file';
        },

        /** 拖动右侧面板左边缘调整宽度（面板贴右，所以向左拖是变宽） */
        startRightResize(e) {
            const startX = e.clientX;
            const startW = this.rightPanelWidth;
            const maxW = Math.max(320, Math.round(window.innerWidth * 0.7));
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';

            const onMove = (ev) => {
                const w = Math.max(300, Math.min(maxW, startW - (ev.clientX - startX)));
                this.rightPanelWidth = w;
            };
            const onUp = () => {
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
                localStorage.setItem('rightPanelWidth', String(this.rightPanelWidth));
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
            };
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        },

        // ── 页面预览（Agent 生成的页面）──────────────────────────────────────

        async loadPages() {
            this.pagesLoading = true;
            try {
                const r = await fetch(API + '/pages');
                const d = await r.json();
                if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
                this.pagesList = d.pages || [];
                this.pagesDir = d.dir || '';
                this.pagesWorkspace = d.workspace_name || '';
                // 预览中的文件没了（Agent 重建目录 / 用户删掉）→ 退出预览态
                if (this.pagePreviewName &&
                    !this.pagesList.some(p => p.name === this.pagePreviewName)) {
                    this.pagePreviewName = '';
                }
                // 还在预览就重新挂载一次，确保看到的是最新内容
                if (this.pagePreviewName) this.pagePreviewKey++;
            } catch (e) {
                this.pagesList = [];
                this.showToast('读取页面列表失败：' + e.message, 'error');
            } finally {
                this.pagesLoading = false;
            }
        },

        previewPage(p) {
            if (p.name === this.pagePreviewName) {
                this.refreshPreview();
                return;
            }
            this.pagePreviewName = p.name;
            this.pagePreviewKey++;
        },

        refreshPreview() {
            if (!this.pagePreviewName) return;
            this.pagePreviewKey++;
        },

        /** 收起/展开页面列表：收起后 rp-stage 的 flex:1 会自然占满整栏 */
        togglePagesList() {
            this.pagesListCollapsed = !this.pagesListCollapsed;
            try {
                localStorage.setItem('pagesListCollapsed', this.pagesListCollapsed ? '1' : '0');
            } catch (e) { /* 隐私模式下写不了，忽略即可 */ }
        },

        // ── 元素选取（「编辑页面」）──────────────────────────────────────────
        //
        // 预览 iframe 带 sandbox 且不含 allow-same-origin，所以父页面读不到它的
        // DOM，也无法把点击事件派发进去。做法是：编辑模式下让后端往页面里注入
        // picker.js（页面内部自己监听悬停/点击），再由它 postMessage 回传选中元素。

        togglePageEditMode() {
            if (!this.isPreviewablePage) {
                this.showToast('只有 HTML 页面支持元素选取', 'info');
                return;
            }
            this.pageEditMode = !this.pageEditMode;
            this.pagePreviewKey++;   // 重载 iframe：带上/去掉 ?edit=1
            this.showToast(this.pageEditMode
                ? '元素选取已开启：在预览里点选元素，Esc 退出'
                : '已退出元素选取', 'info');
        },

        exitPageEditMode() {
            if (!this.pageEditMode) return;
            this.pageEditMode = false;
            this.pagePreviewKey++;
        },

        /**
         * 收到页面内 picker.js 回传的选中元素 → 生成一个「元素标签」。
         *
         * 标签只负责「记住选了哪些元素」，不往输入框里塞文本 —— 输入框留给用户
         * 写修改要求。可并存多个标签，每个都能单独删除；
         * 发送时由 buildPickBlocks() 把标签转成文本块交给 agent。
         */
        onElementPicked(info) {
            if (!info || !info.label) return;

            const page = this.pagePreviewName || '';
            // 同一个元素重复点选就不再加一个（避免误点产生重复标签）
            const dup = this.pagePicks.find(p => p.page === page && p.label === info.label &&
                                                 p.width === info.width && p.height === info.height);
            if (!dup) {
                this.pagePicks.push({
                    id: 'pick' + (++this._pickSeq),
                    page: page,
                    label: info.label,
                    width: info.width,
                    height: info.height,
                    text: info.text || '',
                });
            }

            this.$nextTick(() => {
                const el = this.$refs.chatInput;
                if (el) el.focus();
            });
            const n = this.pagePicks.length;
            this.showToast(dup
                ? '该元素已在标签里（共 ' + n + ' 个）'
                : '已添加元素标签：' + info.label + '（共 ' + n + ' 个），在输入框写修改要求',
                dup ? 'info' : 'success');
        },

        /** 删除单个元素标签 */
        removePick(id) {
            this.pagePicks = this.pagePicks.filter(p => p.id !== id);
        },

        /** 清空全部元素标签 */
        clearPicks() {
            this.pagePicks = [];
        },

        /**
         * 把元素标签转成给 agent 的文本块。
         *
         * 【为什么每个标签都自带一段「修改要求」】块要与系统提示词里约定的
         * 三行格式完全一致（页面 / 元素名 尺寸 / 修改要求），这样每段都是自包含的，
         * agent 不需要跨段推断「这段要求管哪几个元素」。代价是要求文字会重复 N 遍，
         * 换来的是解析上不会出错。
         */
        buildPickBlocks(requirement) {
            if (!this.pagePicks.length) return '';
            const req = (requirement || '').trim();
            return this.pagePicks.map(p =>
                '【页面编辑】' + p.page + '\n' +
                p.label + '  ' + p.width + ' × ' + p.height + '\n' +
                '修改要求：' + req
            ).join('\n\n');
        },

        openPageExternal() {
            if (!this.canPreviewFrame) return;
            // 新窗口里不需要选取脚本，刻意不带 edit=1
            const path = (this.pagePreviewName || '').split('/').map(encodeURIComponent).join('/');
            window.open(API + '/pages/file/' + path, '_blank', 'noopener');
        },

        deletePage(p) {
            this.confirmTitle = '删除页面文件';
            this.confirmDesc = '将从当前工作区的 pages/ 目录删除「' + p.name + '」，此操作不可恢复。';
            this.confirmBtnText = '删除';
            this.confirmCallback = async () => {
                try {
                    const path = p.name.split('/').map(encodeURIComponent).join('/');
                    const r = await fetch(API + '/pages/file/' + path, { method: 'DELETE' });
                    if (!r.ok) {
                        const err = await r.json().catch(() => ({}));
                        throw new Error(err.detail || ('HTTP ' + r.status));
                    }
                    if (this.pagePreviewName === p.name) this.pagePreviewName = '';
                    this.pagesList = this.pagesList.filter(x => x.name !== p.name);
                    this.showToast('已删除 ' + p.name, 'success');
                } catch (e) {
                    this.showToast('删除失败：' + e.message, 'error');
                }
            };
            this.showConfirm = true;
        },

        openKnowledge() {
            this.currentView = 'knowledge';
            this.loadKbDocuments();
        },

        async loadKbDocuments() {
            try {
                const r = await fetch(API + '/kb/documents');
                if (r.ok) {
                    const d = await r.json();
                    this.kbDocuments = d.documents || [];
                }
            } catch (e) {
                this.kbDocuments = [];
            }
        },

        async onKbFileSelected(e) {
            const files = Array.from(e.target.files);
            this.uploadKbFiles(files);
            e.target.value = '';
        },

        async onKbDrop(e) {
            this.kbDragOver = false;
            const files = Array.from(e.dataTransfer.files);
            if (files.length > 0) this.uploadKbFiles(files);
        },

        async uploadKbFiles(files) {
            const MAX_SIZE = 50 * 1024 * 1024;
            for (const f of files) {
                if (f.size > MAX_SIZE) {
                    this.showToast(f.name + ' 超过50MB限制', 'error');
                    continue;
                }
                const fd = new FormData();
                fd.append('file', f);
                try {
                    this.showToast('正在上传 ' + f.name + '...', 'info');
                    const r = await fetch(API + '/kb/upload', { method: 'POST', body: fd });
                    if (r.ok) {
                        const d = await r.json();
                        this.kbDocuments.push(d.document);
                        this.showToast(f.name + ' 上传成功', 'success');
                    } else {
                        const err = await r.json().catch(() => ({}));
                        this.showToast(err.error || f.name + ' 上传失败', 'error');
                    }
                } catch (e) {
                    this.showToast(f.name + ' 上传失败', 'error');
                }
            }
        },

        viewDoc(doc) {
            this.kbViewDoc = doc;
            fetch(API + '/kb/' + doc.id)
                .then(r => r.json())
                .then(d => { this.kbViewDoc = { ...d.document }; })
                .catch(() => {});
        },

        async deleteDoc(doc) {
            this.confirmTitle = '删除文档';
            this.confirmDesc = '删除后将无法恢复该文档。';
            this.confirmBtnText = '删除';
            this.confirmCallback = async () => {
                try {
                    await fetch(API + '/kb/' + doc.id, { method: 'DELETE' });
                    this.kbDocuments = this.kbDocuments.filter(d => d.id !== doc.id);
                    this.showToast('已删除', 'success');
                } catch (e) {
                    this.showToast('删除失败', 'error');
                }
            };
            this.showConfirm = true;
        },

        getDocIcon(fn) {
            const e = (fn || '').toLowerCase().split('.').pop();
            const map = {
                pdf: 'fa-file-pdf',
                doc: 'fa-file-word', docx: 'fa-file-word',
                xls: 'fa-file-excel', xlsx: 'fa-file-excel',
                ppt: 'fa-file-powerpoint', pptx: 'fa-file-powerpoint',
                txt: 'fa-file-lines', md: 'fa-file-lines',
                csv: 'fa-table',
                json: 'fa-cubes',
                html: 'fa-html5', htm: 'fa-html5',
                xml: 'fa-code', yaml: 'fa-code', yml: 'fa-code',
                png: 'fa-image', jpg: 'fa-image', jpeg: 'fa-image',
                gif: 'fa-image', webp: 'fa-image', bmp: 'fa-image', svg: 'fa-image'
            };
            return map[e] || 'fa-file';
        },

        getDocIconClass(fn) {
            const e = (fn || '').toLowerCase().split('.').pop();
            const cls = {
                pdf: 'pdf', doc: 'doc', docx: 'doc',
                xls: 'sheet', xlsx: 'sheet',
                ppt: 'slide', pptx: 'slide',
                txt: 'txt', md: 'txt',
                csv: 'sheet',
                json: 'other', html: 'other', htm: 'other',
                xml: 'other', yaml: 'other', yml: 'other',
                png: 'img', jpg: 'img', jpeg: 'img',
                gif: 'img', webp: 'img', bmp: 'img', svg: 'img'
            };
            return cls[e] || 'other';
        },

        formatDate(ts) {
            if (!ts) return '';
            const d = new Date(ts);
            const pad = n => String(n).padStart(2, '0');
            return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
        },

        /**
         * 会话记录按工作区分别保存。
         *
         * 每个工作区一份 localStorage 键：chatSessions::<workspaceId>。
         * 没有激活工作区时退回应用目录的那一份（键为 chatSessions::app），
         * 保证"未选工作区"状态下的对话不会和某个工作区混在一起。
         */
        sessionsKey(suffix) {
            const id = this.workspaces.active_id || 'app';
            return (suffix || 'chatSessions') + '::' + id;
        },

        /** 一次性把旧的单一 chatSessions 迁移到「应用目录」这个工作区下 */
        migrateLegacySessions() {
            try {
                if (localStorage.getItem('chatSessions') === null) return;
                const legacyKey = this.sessionsKey('chatSessions');
                if (localStorage.getItem(legacyKey) === null) {
                    localStorage.setItem(legacyKey, localStorage.getItem('chatSessions'));
                }
                localStorage.removeItem('chatSessions');

                // lastSessionId 也从全局键迁移到当前工作区的键
                const legacyLast = localStorage.getItem('lastSessionId');
                if (legacyLast) {
                    localStorage.setItem(this.sessionsKey('lastSessionId'), legacyLast);
                    localStorage.removeItem('lastSessionId');
                }
            } catch (e) { /* 隐私模式等场景忽略 */ }
        },

        loadSessions() {
            try {
                const d = localStorage.getItem(this.sessionsKey('chatSessions'));
                if (d) {
                    this.sessions = JSON.parse(d).map(s => ({
                        id: s.id,
                        title: s.title || '未命名',
                        messages: s.messages || [],
                        createdAt: s.createdAt || Date.now(),
                        updatedAt: s.updatedAt || s.createdAt || Date.now(),
                        // 老数据没有 scope 字段，回落到工作区根
                        scope: s.scope || '',
                        pinned: s.pinned || false,
                        archived: s.archived || false,
                    }));
                } else {
                    this.sessions = [];
                }
            } catch (e) {
                this.sessions = [];
            }
        },

        saveSessions() {
            try {
                const key = this.sessionsKey('chatSessions');
                localStorage.setItem(key, JSON.stringify(this.sessions));
                if (this.sessionId) localStorage.setItem(this.sessionsKey('lastSessionId'), this.sessionId);
                // 同步刷新侧边栏缓存，让新对话／新标题立刻出现
                const wsId = this.workspaces.active_id || 'app';
                this.wsSessions = { ...this.wsSessions, [wsId]: this.readSessionsFor(wsId) };
                this._wsSessionsVersion = (this._wsSessionsVersion || 0) + 1;
            } catch (e) {}
        }
    }
});

/**
 * 兜底：组件渲染 / 生命周期里抛错时，Vue 默认会把整棵树卸载掉 ——
 * 用户看到的就是**整页白屏**，且屏幕上没有任何错误信息，只能靠翻控制台才知道原因。
 * （真实事故：浏览器缓存了旧的 script.js、而 index.html 是新的，模板引用了旧代码里
 *  不存在的字段，渲染直接抛错 → 白屏。）
 *
 * 这里改成"记录 + 明确提示 + 可一键刷新"，宁可显示一条错误横幅，也不要一片空白。
 */
function showFatalNotice(message) {
    let el = document.getElementById('fatal-notice');
    if (!el) {
        el = document.createElement('div');
        el.id = 'fatal-notice';
        el.className = 'fatal-notice';
        const title = document.createElement('b');
        title.textContent = '界面出错了';
        const text = document.createElement('span');
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = '强制刷新';
        // 用全新 URL 重新加载，绕过可能串了版本的缓存
        btn.onclick = () => { location.href = location.pathname + '?_=' + Date.now(); };
        el.appendChild(title);
        el.appendChild(text);
        el.appendChild(btn);
        document.body.appendChild(el);
    }
    el.querySelector('span').textContent = message;
}

let __errorNotified = false;

/**
 * 致命渲染错误时自动重新加载一次（带新 URL，绕开缓存）。
 *
 * 【为什么值得这么做】这类错误最常见的原因就是资源版本串号（HTML 新、JS 旧），
 * 重新加载一次即可自愈，用户甚至不会察觉。用 sessionStorage 做 20 秒节流，
 * 保证"最多自动重试一次"，避免真存在 bug 时陷入无限刷新。
 */
function tryAutoRecover() {
    try {
        const key = 'uiAutoRecoverAt';
        const last = Number(sessionStorage.getItem(key) || 0);
        if (Date.now() - last < 20000) return false;
        sessionStorage.setItem(key, String(Date.now()));
        return true;
    } catch (e) {
        return false;   // 无痕模式等取不到 sessionStorage：不自动重试，直接提示
    }
}

__app.config.errorHandler = function (err, instance, info) {
    const msg = (err && err.message) ? err.message : String(err);
    console.error('[UI] 组件错误 @' + info + ':', err);
    if (__errorNotified) return;   // 渲染可能反复失败，只处理一次
    __errorNotified = true;
    if (tryAutoRecover()) {
        console.warn('[UI] 尝试自动重新加载以恢复…');
        location.href = location.pathname + '?_r=' + Date.now();
        return;
    }
    const tip = '渲染或逻辑出错（' + info + '）：' + msg + ' —— 请点右侧按钮强制刷新；若仍不行请把控制台信息反馈';
    try {
        const vm = __app._instance && __app._instance.proxy;
        if (vm && vm.showToast) vm.showToast(tip, 'error');
    } catch (e) { /* 提示失败不影响下面的横幅 */ }
    showFatalNotice(tip);
};

__app.mount('#app');