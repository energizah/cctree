/**
 * Claude Code Plugin for Canvas Chat
 *
 * Bridges Canvas Chat's visual canvas to the Claude Code CLI backend.
 * Uses fork-per-message to enable branching from any historical node.
 *
 * Slash commands:
 *   /cc <prompt>          — send a message via Claude Code CLI
 *   /cc-import [session]  — import a JSONL session onto the canvas
 *   /cc-import-all        — import all sessions from the current cwd onto the canvas
 *   /cc-sessions          — list available Claude Code sessions
 *   /cc-cwd [path]        — set/show the working directory for Claude Code
 */

import { FeaturePlugin } from '/static/js/feature-plugin.js';
import { BaseNode } from '/static/js/node-protocols.js';
import { NodeRegistry } from '/static/js/node-registry.js';
import { NodeType, EdgeType, createNode, createEdge } from '/static/js/graph-types.js';
import { readSSEStream } from '/static/js/sse.js';
import { apiUrl, formatUserError } from '/static/js/utils.js';

// ---------------------------------------------------------------------------
// Fork index — maps canvas node IDs to Claude Code session metadata
// ---------------------------------------------------------------------------

class ForkIndex {
    constructor() {
        this.nodes = {};       // nodeId -> { sessionId, claudeUuid, forkSessionId }
        this.activeSessionId = null;
        this.cwd = null;
    }

    set(nodeId, entry) {
        this.nodes[nodeId] = entry;
    }

    get(nodeId) {
        return this.nodes[nodeId] || null;
    }

    toJSON() {
        return {
            nodes: this.nodes,
            activeSessionId: this.activeSessionId,
            cwd: this.cwd,
        };
    }

    static fromJSON(data) {
        const idx = new ForkIndex();
        if (!data) return idx;
        idx.nodes = data.nodes || {};
        idx.activeSessionId = data.activeSessionId || null;
        idx.cwd = data.cwd || null;
        return idx;
    }
}

// ---------------------------------------------------------------------------
// SessionTreeNode — custom node type for interactive session trees
// ---------------------------------------------------------------------------

class SessionTreeNode extends BaseNode {
    getTypeLabel() {
        return 'Session Tree';
    }

    getTypeIcon() {
        return '';
    }

    getSummaryText(canvas) {
        const title = this.node.title || 'Session Tree';
        return canvas.truncate(title, 50);
    }

    renderContent(canvas) {
        const treeLines = this.node.treeLines || [];
        const title = this.node.title || 'Session Tree';
        const sessionCount = this.node.sessionCount || 0;
        const nodeCount = this.node.nodeCount || 0;

        let html = `<div class="cc-session-tree">`;
        html += `<div class="cc-tree-header">${canvas.escapeHtml(title)}</div>`;
        html += `<div class="cc-tree-stats">${sessionCount} sessions, ${nodeCount} messages</div>`;

        for (const line of treeLines) {
            const sessionId = line.session_ids?.[0] || '';
            const dataAttr = sessionId ? ` data-session-id="${canvas.escapeHtml(sessionId)}"` : '';
            const clickClass = sessionId ? ' cc-tree-clickable' : '';

            const prefix = canvas.escapeHtml(line.prefix + line.connector + ' ');
            const text = canvas.escapeHtml(line.text);
            const countBadge = line.count > 1
                ? `<span class="cc-tree-count">\u00d7${line.count}</span>`
                : '';

            html += `<div class="cc-tree-line${clickClass}"${dataAttr}>`;
            html += `<span class="cc-tree-prefix">${prefix}</span>`;
            html += `<span class="cc-tree-text">${text}</span>`;
            html += countBadge;
            html += `</div>`;
        }

        html += `</div>`;
        return html;
    }

    getActions() {
        return [];
    }

    getEventBindings() {
        return [
            {
                selector: '.cc-tree-line.cc-tree-clickable',
                multiple: true,
                handler: (nodeId, e, canvas) => {
                    const sessionId = e.currentTarget.dataset.sessionId;
                    if (sessionId) {
                        canvas.emit('cc-import-session', nodeId, sessionId);
                    }
                },
            },
        ];
    }

    isContentEditable() {
        return false;
    }
}

NodeRegistry.register({
    type: 'session-tree',
    protocol: SessionTreeNode,
    defaultSize: { width: 800, height: 600 },
    css: `
        .cc-session-tree {
            font-family: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace;
            font-size: 12px;
            padding: 12px;
            line-height: 1.6;
        }
        .cc-tree-header {
            font-size: 14px;
            font-weight: 600;
            margin-bottom: 4px;
            color: var(--text-color);
        }
        .cc-tree-stats {
            font-size: 11px;
            color: var(--text-muted);
            margin-bottom: 12px;
        }
        .cc-tree-line {
            white-space: pre;
            padding: 1px 4px;
            border-radius: 3px;
        }
        .cc-tree-line.cc-tree-clickable {
            cursor: pointer;
        }
        .cc-tree-line.cc-tree-clickable:hover {
            background: #313244;
        }
        .cc-tree-prefix {
            color: #585b70;
        }
        .cc-tree-text {
            color: var(--text-color);
        }
        .cc-tree-count {
            color: var(--text-muted);
            margin-left: 8px;
            font-size: 11px;
        }
    `,
});

// ---------------------------------------------------------------------------
// ClaudeCodeFeature
// ---------------------------------------------------------------------------

class ClaudeCodeFeature extends FeaturePlugin {
    constructor(context) {
        super(context);
        this.forkIndex = new ForkIndex();
        this._pendingForks = new Map(); // nodeId -> Promise
        this._toolStatus = new Map();   // nodeId -> current tool status element
        this._collapsedMessages = new Map(); // nodeId -> collapsed_messages array
    }

    getSlashCommands() {
        return [
            {
                command: '/cc',
                description: 'Send a message via Claude Code CLI',
                placeholder: 'prompt...',
            },
            {
                command: '/cc-import',
                description: 'Import a Claude Code session onto the canvas',
                placeholder: 'session-id (leave empty for picker)',
            },
            {
                command: '/cc-import-all',
                description: 'Import all sessions from the current working directory',
            },
            {
                command: '/cc-sessions',
                description: 'List available Claude Code sessions',
            },
            {
                command: '/cc-cwd',
                description: 'Set/show the Claude Code working directory',
                placeholder: 'path (leave empty to show current)',
            },
        ];
    }

    async onLoad() {
        this._loadIndex();

        this.injectCSS(`
            .cc-tool-status {
                position: absolute;
                bottom: 4px;
                left: 8px;
                right: 8px;
                padding: 3px 8px;
                border-radius: 3px;
                font-size: 11px;
                font-family: monospace;
                background: #1a2a1a;
                color: #6a9955;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
                pointer-events: none;
                opacity: 0.9;
                transition: opacity 0.3s;
                z-index: 5;
            }
            .cc-tool-status.fade-out {
                opacity: 0;
            }
            .cc-mode-indicator {
                display: inline-flex;
                align-items: center;
                gap: 4px;
                padding: 2px 8px;
                border-radius: 3px;
                font-size: 11px;
                font-family: monospace;
                background: #1e1e2e;
                color: #89b4fa;
                cursor: pointer;
                user-select: none;
                border: 1px solid #313244;
                margin-left: 6px;
            }
            .cc-mode-indicator:hover {
                background: #313244;
            }
            .cc-mode-indicator .cc-dot {
                width: 6px;
                height: 6px;
                border-radius: 50%;
                background: #a6e3a1;
            }
            .cc-mode-indicator .cc-dot.inactive {
                background: #585b70;
            }
            .cc-fork-badge {
                position: absolute;
                top: 4px;
                right: 4px;
                width: 8px;
                height: 8px;
                border-radius: 50%;
                z-index: 5;
                pointer-events: none;
            }
            .cc-fork-badge.ready {
                background: #a6e3a1;
                box-shadow: 0 0 4px #a6e3a180;
            }
            .cc-fork-badge.pending {
                background: #f9e2af;
                animation: cc-pulse 1.5s ease-in-out infinite;
            }
            .cc-fork-badge.failed {
                background: #f38ba8;
            }
            @keyframes cc-pulse {
                0%, 100% { opacity: 0.4; }
                50% { opacity: 1; }
            }
            .cc-expand-btn {
                position: absolute;
                top: 4px;
                left: 4px;
                padding: 2px 8px;
                border-radius: 3px;
                font-size: 10px;
                font-family: monospace;
                background: #89b4fa;
                color: #1e1e2e;
                border: none;
                cursor: pointer;
                z-index: 10;
                transition: background 0.15s;
                font-weight: bold;
            }
            .cc-expand-btn:hover {
                background: #b4d0fb;
            }
        `);

        this._addModeIndicator();
        this._installSendIntercept();
        this._installExpandHandler();

        console.log('[ClaudeCode] Plugin loaded');
    }

    // -- Expand collapsed segments on double-click ----------------------------

    _installExpandHandler() {
        // Watch for new nodes being rendered and add expand buttons to collapsed ones
        const observer = new MutationObserver(() => {
            this._addExpandButtons();
        });

        const container = document.querySelector('.canvas-container') ||
                          document.querySelector('svg');
        if (container) {
            observer.observe(container, { childList: true, subtree: true });
        }
    }

    _addExpandButtons() {
        console.log(`[ClaudeCode] _addExpandButtons: ${this._collapsedMessages.size} collapsed nodes, ${this.canvas.nodeElements?.size} rendered nodes`);
        for (const [nodeId, msgs] of this._collapsedMessages) {
            this._addExpandButton(nodeId, msgs);
        }
    }

    _addExpandButton(nodeId, msgs) {
        const wrapper = this.canvas.nodeElements?.get(nodeId);
        if (!wrapper) {
            console.log(`[ClaudeCode] No wrapper for ${nodeId}`);
            return;
        }
        if (wrapper.querySelector('.cc-expand-btn')) return;

        console.log(`[ClaudeCode] Adding expand button to ${nodeId}`, wrapper.tagName, wrapper.innerHTML?.slice(0, 100));
        const nodeDiv = wrapper.querySelector('.node') || wrapper;
        nodeDiv.style.position = 'relative';

        const btn = document.createElement('button');
        btn.className = 'cc-expand-btn';
        btn.textContent = `Expand ${msgs.length} messages`;
        btn.title = 'Expand collapsed segment into individual messages';
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            this._expandCollapsed(nodeId, msgs);
        });

        nodeDiv.appendChild(btn);
    }

    _expandCollapsed(collapsedNodeId, messages) {
        const collapsedNode = this.graph.getNode(collapsedNodeId);
        if (!collapsedNode) return;

        // Find incoming and outgoing edges
        const allEdges = this.graph.getAllEdges?.() || this.graph.edges || [];
        const incomingEdges = allEdges.filter(e => e.target === collapsedNodeId);
        const outgoingEdges = allEdges.filter(e => e.source === collapsedNodeId);

        const baseX = collapsedNode.position?.x || 0;
        const baseY = collapsedNode.position?.y || 0;
        const NODE_GAP_Y = 40;

        // Create individual nodes
        const newNodeIds = [];
        let yOffset = 0;

        for (const msg of messages) {
            const node = createNode(msg.type, msg.content, {
                position: { x: baseX, y: baseY + yOffset },
                width: collapsedNode.width || 400,
                model: msg.model,
            });
            if (msg.timestamp) {
                node.created_at = new Date(msg.timestamp).getTime();
            }

            this.graph.addNode(node);
            newNodeIds.push(node.id);

            if (msg.session_id) {
                this.forkIndex.set(node.id, {
                    sessionId: msg.session_id,
                    forkSessionId: null,
                });
            }

            // Estimate height
            const lines = Math.max(3, Math.min(20, Math.floor(msg.content.length / 60) + 1));
            const height = lines * 24 + 60;
            yOffset += height + NODE_GAP_Y;
        }

        // Chain new nodes together
        for (let i = 1; i < newNodeIds.length; i++) {
            const edge = createEdge(newNodeIds[i - 1], newNodeIds[i], EdgeType.REPLY);
            this.graph.addEdge(edge);
        }

        // Reconnect incoming edges to the first new node
        for (const e of incomingEdges) {
            this.graph.removeEdge(e.id);
            const newEdge = createEdge(e.source, newNodeIds[0], e.type);
            this.graph.addEdge(newEdge);
        }

        // Reconnect outgoing edges from the last new node
        for (const e of outgoingEdges) {
            this.graph.removeEdge(e.id);
            const newEdge = createEdge(newNodeIds[newNodeIds.length - 1], e.target, e.type);
            this.graph.addEdge(newEdge);
        }

        // Remove the collapsed node
        this.graph.removeNode(collapsedNodeId);
        this._collapsedMessages.delete(collapsedNodeId);

        this._saveIndex();
        this.saveSession();
        this.showToast?.(`Expanded ${messages.length} messages`);
    }

    // -- Canvas event handlers for custom node types --------------------------

    getCanvasEventHandlers() {
        return {
            'cc-import-session': (_nodeId, sessionId) => {
                this._importSession(sessionId);
            },
        };
    }

    // -- Auto-route replies to Claude Code nodes ------------------------------

    _isClaudeCodeNode(nodeId) {
        // Check fork index (AI nodes with session metadata)
        if (this.forkIndex.get(nodeId)) return true;
        // Check if the node was created by this plugin (model field)
        const node = this.graph.getNode(nodeId);
        if (node?.model === 'claude-code') return true;
        // Check if any parent is a CC node (handles human nodes in CC chains)
        if (node) {
            const parents = this.graph.getParents(nodeId);
            for (const parent of parents) {
                if (parent?.model === 'claude-code' || this.forkIndex.get(parent.id)) {
                    return true;
                }
            }
        }
        return false;
    }

    _installSendIntercept() {
        const app = window.app;
        if (!app) return;

        const origHandleSend = app.handleSend.bind(app);
        const feature = this;

        app.handleSend = async function () {
            const content = app.chatInput.value.trim();
            if (!content || content.startsWith('/')) {
                // Slash commands and empty input — use original handler
                return origHandleSend();
            }

            // Check if any selected node is a Claude Code node
            const selectedIds = app.canvas.getSelectedNodeIds();
            const isCC = selectedIds.some(id => feature._isClaudeCodeNode(id));

            if (isCC) {
                // Route through /cc — clear input and call handleCC directly
                app.chatInput.value = '';
                app.chatInput.style.height = 'auto';
                await feature.handleCC('/cc', content, null);
                return;
            }

            return origHandleSend();
        };
    }

    // -- Mode indicator in the toolbar --------------------------------------

    _addModeIndicator() {
        // Find the toolbar area (near the model picker)
        const toolbar = document.querySelector('.toolbar-right') ||
                        document.querySelector('.model-picker')?.parentElement;
        if (!toolbar) return;

        const indicator = document.createElement('span');
        indicator.className = 'cc-mode-indicator';
        indicator.title = 'Claude Code mode — click to set working directory';
        indicator.innerHTML = '<span class="cc-dot"></span>CC';

        indicator.addEventListener('click', () => {
            const cwd = prompt('Claude Code working directory:', this.forkIndex.cwd || '');
            if (cwd !== null) {
                this.forkIndex.cwd = cwd || null;
                this._saveIndex();
                this.showToast?.(cwd ? `Working directory: ${cwd}` : 'Working directory cleared');
            }
        });

        toolbar.appendChild(indicator);
        this._modeIndicator = indicator;
    }

    // -- Tool status overlay on streaming nodes -----------------------------

    _showToolStatus(nodeId, text) {
        const wrapper = this.canvas.nodeElements?.get(nodeId);
        if (!wrapper) return;

        let el = this._toolStatus.get(nodeId);
        if (!el) {
            el = document.createElement('div');
            el.className = 'cc-tool-status';
            const nodeDiv = wrapper.querySelector('.node') || wrapper;
            nodeDiv.style.position = 'relative';
            nodeDiv.appendChild(el);
            this._toolStatus.set(nodeId, el);
        }

        el.textContent = text;
        el.classList.remove('fade-out');
    }

    _clearToolStatus(nodeId) {
        const el = this._toolStatus.get(nodeId);
        if (el) {
            el.classList.add('fade-out');
            setTimeout(() => {
                el.remove();
                this._toolStatus.delete(nodeId);
            }, 300);
        }
    }

    // -- Fork status badge on nodes -----------------------------------------

    _setForkBadge(nodeId, state) {
        const wrapper = this.canvas.nodeElements?.get(nodeId);
        if (!wrapper) return;

        let badge = wrapper.querySelector('.cc-fork-badge');
        if (!badge) {
            badge = document.createElement('div');
            badge.className = 'cc-fork-badge';
            const nodeDiv = wrapper.querySelector('.node') || wrapper;
            nodeDiv.style.position = 'relative';
            nodeDiv.appendChild(badge);
        }

        badge.className = `cc-fork-badge ${state}`;
    }

    // -- Index persistence --------------------------------------------------

    _loadIndex() {
        try {
            const session = this.storage?.currentSession;
            if (session?.claudeCodeIndex) {
                this.forkIndex = ForkIndex.fromJSON(session.claudeCodeIndex);
            }
        } catch (e) {
            console.warn('[ClaudeCode] Failed to load fork index:', e);
        }
    }

    _saveIndex() {
        try {
            const session = this.storage?.currentSession;
            if (session) {
                session.claudeCodeIndex = this.forkIndex.toJSON();
                this.saveSession();
            }
        } catch (e) {
            console.warn('[ClaudeCode] Failed to save fork index:', e);
        }
    }

    // -- /cc <prompt> -------------------------------------------------------

    async handleCC(_command, args, context) {
        const prompt = args?.trim();
        if (!prompt) {
            this.showToast?.('Usage: /cc <prompt>');
            return;
        }

        // Determine parent node(s)
        const selectedIds = this.canvas.getSelectedNodeIds();
        const parentIds = selectedIds.length > 0 ? selectedIds : [];

        // Look up the fork session for the parent node (for branching)
        let resumeSessionId = null;
        if (parentIds.length === 1) {
            const entry = this.forkIndex.get(parentIds[0]);
            if (entry?.forkSessionId) {
                resumeSessionId = entry.forkSessionId;
            } else if (entry?.sessionId && !entry?.forkSessionId) {
                // Fork is still pending — wait for it
                const pending = this._pendingForks.get(parentIds[0]);
                if (pending) {
                    this.showToast?.('Waiting for fork to complete...');
                    await pending;
                    const updated = this.forkIndex.get(parentIds[0]);
                    if (updated?.forkSessionId) {
                        resumeSessionId = updated.forkSessionId;
                    }
                }
            }
        }
        // Fall back to the active session if no fork found
        if (!resumeSessionId) {
            resumeSessionId = this.forkIndex.activeSessionId;
        }

        // Create human node
        const humanNode = createNode(NodeType.HUMAN, prompt, {
            position: this.graph.autoPosition(parentIds),
        });
        this.graph.addNode(humanNode);

        // Create edges from parents
        for (const parentId of parentIds) {
            const edge = createEdge(
                parentId,
                humanNode.id,
                parentIds.length > 1 ? EdgeType.MERGE : EdgeType.REPLY,
            );
            this.graph.addEdge(edge);
            this.updateCollapseButtonForNode?.(parentId);
        }

        this.canvas.clearSelection();
        this.canvas.selectNode(humanNode.id);

        // Create AI node
        const aiNode = createNode(NodeType.AI, '', {
            position: this.graph.autoPosition([humanNode.id]),
            model: 'claude-code',
        });
        this.graph.addNode(aiNode);

        const aiEdge = createEdge(humanNode.id, aiNode.id, EdgeType.REPLY);
        this.graph.addEdge(aiEdge);
        this.updateCollapseButtonForNode?.(humanNode.id);

        // Stream Claude Code response
        const abortController = new AbortController();
        this.streamingManager.register(aiNode.id, {
            abortController,
            featureId: 'claude-code',
            context: { prompt, sessionId: resumeSessionId },
        });

        await this._streamChat(aiNode.id, abortController, {
            prompt,
            session_id: resumeSessionId,
            cwd: this.forkIndex.cwd,
        });
    }

    async _streamChat(nodeId, abortController, body) {
        try {
            const response = await fetch(apiUrl('/api/claude-code/chat'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
                signal: abortController.signal,
            });

            if (!response.ok) {
                const detail = await response.json().catch(() => ({}));
                throw new Error(detail.detail || `HTTP ${response.status}`);
            }

            let fullContent = '';
            let metadata = {};

            await readSSEStream(response, {
                onEvent: (eventType, data) => {
                    switch (eventType) {
                        case 'message':
                            if (data) {
                                fullContent += data;
                                this.canvas.updateNodeContent(nodeId, fullContent, true);
                                this.graph.updateNode(nodeId, { content: fullContent });
                            }
                            break;
                        case 'status':
                            this._showToolStatus(nodeId, data);
                            break;
                        case 'done':
                            if (data) {
                                try {
                                    metadata = JSON.parse(data);
                                } catch { /* ignore */ }
                            }
                            break;
                    }
                },
                onDone: () => {
                    this._clearToolStatus(nodeId);
                    this.streamingManager.unregister(nodeId);
                    this.canvas.updateNodeContent(nodeId, fullContent, false);
                    this.graph.updateNode(nodeId, { content: fullContent });

                    // Append cost info as a subtle suffix
                    if (metadata.cost_usd != null) {
                        const cost = `$${metadata.cost_usd.toFixed(4)}`;
                        this.graph.updateNode(nodeId, {
                            title: `Claude Code (${cost})`,
                        });
                    }

                    const sessionId = metadata.session_id;
                    if (sessionId) {
                        this.forkIndex.activeSessionId = sessionId;
                        this.forkIndex.set(nodeId, {
                            sessionId,
                            forkSessionId: null,
                        });
                        this._setForkBadge(nodeId, 'pending');
                        this._forkAndStore(nodeId, sessionId);
                    }

                    this._saveIndex();
                    this.saveSession();
                    this.generateNodeSummary?.(nodeId);
                },
                onError: (err) => {
                    this._clearToolStatus(nodeId);
                    this.streamingManager.unregister(nodeId);
                    this._handleStreamError(nodeId, fullContent, err, body);
                },
            });
        } catch (err) {
            this._clearToolStatus(nodeId);
            if (err.name === 'AbortError') {
                console.log(`[ClaudeCode] Stream aborted for node ${nodeId}`);
                this.streamingManager.unregister(nodeId);
                // Preserve partial content
                if (this.graph.getNode(nodeId)?.content) {
                    this.canvas.updateNodeContent(
                        nodeId,
                        this.graph.getNode(nodeId).content,
                        false,
                    );
                }
                this.saveSession();
                return;
            }
            console.error('[ClaudeCode] Stream error:', err);
            this.streamingManager.unregister(nodeId);
            this._handleStreamError(nodeId, '', err, body);
        }
    }

    _handleStreamError(nodeId, partialContent, err, requestBody) {
        const errorInfo = formatUserError(err);
        const errorContent = partialContent
            ? `${partialContent}\n\n---\n**${errorInfo.title}:** ${errorInfo.description}`
            : `**${errorInfo.title}**\n\n${errorInfo.description}`;

        this.canvas.updateNodeContent(nodeId, errorContent, false);
        this.graph.updateNode(nodeId, { content: errorContent });

        // Use canvas-chat's native error UI if available (shows retry/dismiss)
        if (this._app?.showNodeError && errorInfo.canRetry) {
            this._app.retryContexts?.set(nodeId, {
                type: 'claude-code',
                body: requestBody,
            });
            this.canvas.showNodeError?.(nodeId, errorInfo);
        }

        this.saveSession();
    }

    async _forkAndStore(nodeId, sessionId) {
        if (this._pendingForks.has(nodeId)) return;

        const promise = (async () => {
            try {
                const resp = await fetch(apiUrl('/api/claude-code/fork'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ session_id: sessionId, cwd: this.forkIndex.cwd }),
                });

                if (!resp.ok) {
                    const detail = await resp.json().catch(() => ({}));
                    console.warn('[ClaudeCode] Fork failed:', detail.detail);
                    this._setForkBadge(nodeId, 'failed');

                    // Fall back: the node can still be used via the original
                    // session ID (linear continuation, no true branch)
                    const entry = this.forkIndex.get(nodeId);
                    if (entry) {
                        entry.forkSessionId = sessionId; // use original as fallback
                        this._saveIndex();
                    }
                    return;
                }

                const data = await resp.json();
                const entry = this.forkIndex.get(nodeId);
                if (entry) {
                    entry.forkSessionId = data.fork_session_id;
                    this._saveIndex();
                    this._setForkBadge(nodeId, 'ready');
                    console.log(`[ClaudeCode] Forked: ${data.fork_session_id}`);
                }
            } catch (err) {
                console.warn('[ClaudeCode] Fork error:', err);
                this._setForkBadge(nodeId, 'failed');

                // Same fallback
                const entry = this.forkIndex.get(nodeId);
                if (entry) {
                    entry.forkSessionId = sessionId;
                    this._saveIndex();
                }
            } finally {
                this._pendingForks.delete(nodeId);
            }
        })();

        this._pendingForks.set(nodeId, promise);
    }

    // -- /cc-import [session-id] --------------------------------------------

    async handleImport(_command, args, _context) {
        const sessionId = args?.trim();

        if (!sessionId) {
            await this._showSessionPicker();
            return;
        }

        await this._importSession(sessionId);
    }

    async _importSession(sessionId) {
        this.showToast?.(`Importing session ${sessionId.slice(0, 8)}...`);

        try {
            const resp = await fetch(apiUrl('/api/claude-code/import'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    session_id: sessionId,
                    cwd: this.forkIndex.cwd,
                }),
            });

            if (!resp.ok) {
                const detail = await resp.json().catch(() => ({}));
                throw new Error(detail.detail || `HTTP ${resp.status}`);
            }

            const { nodes, edges } = await resp.json();

            if (nodes.length === 0) {
                this.showToast?.('No conversation messages found in session.');
                return;
            }

            // Add all nodes and edges to the graph
            for (const nodeData of nodes) {
                const node = createNode(nodeData.type, nodeData.content, {
                    position: nodeData.position,
                    width: nodeData.width,
                    height: nodeData.height,
                    model: nodeData.model,
                    title: nodeData.title,
                });
                node.id = nodeData.id;
                if (nodeData.created_at) {
                    node.created_at = new Date(nodeData.created_at).getTime();
                }

                this.graph.addNode(node);

                if (nodeData.claude_uuid) {
                    this.forkIndex.set(node.id, {
                        sessionId: nodeData.session_id,
                        claudeUuid: nodeData.claude_uuid,
                        forkSessionId: null,
                    });
                }
            }

            for (const edgeData of edges) {
                const edge = createEdge(edgeData.source, edgeData.target, edgeData.type);
                edge.id = edgeData.id;
                this.graph.addEdge(edge);
            }

            this.forkIndex.activeSessionId = sessionId;
            this._saveIndex();
            this.saveSession();

            this.showToast?.(`Imported ${nodes.length} nodes from session.`);
        } catch (err) {
            console.error('[ClaudeCode] Import error:', err);
            this.showToast?.(`Import failed: ${err.message}`);
        }
    }

    // -- /cc-import-all -----------------------------------------------------

    async handleImportAll(_command, _args, _context) {
        if (!this.forkIndex.cwd) {
            this.showToast?.('Set a working directory first: /cc-cwd <path>');
            return;
        }

        this.showToast?.('Building session tree...');

        try {
            const resp = await fetch(apiUrl('/api/claude-code/import-dag'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    cwd: this.forkIndex.cwd,
                    format: 'tree',
                }),
            });

            if (!resp.ok) {
                const detail = await resp.json().catch(() => ({}));
                throw new Error(detail.detail || `HTTP ${resp.status}`);
            }

            const data = await resp.json();

            if (!data.tree_lines || data.tree_lines.length === 0) {
                this.showToast?.('No conversation messages found.');
                return;
            }

            const node = createNode('session-tree', '', {
                position: this.graph.autoPosition([]),
                width: 800,
            });
            node.title = `Session Tree \u2014 ${this.forkIndex.cwd}`;
            node.treeLines = data.tree_lines;
            node.sessionCount = data.session_count;
            node.nodeCount = data.node_count;
            this.graph.addNode(node);
            this.canvas.selectNode(node.id);

            const { session_count, node_count } = data;

            this._saveIndex();
            this.saveSession();
            this.showToast?.(`Tree: ${session_count} sessions, ${node_count} messages`);
        } catch (err) {
            console.error('[ClaudeCode] Import-all error:', err);
            this.showToast?.(`Import failed: ${err.message}`);
        }
    }

    // -- /cc-sessions -------------------------------------------------------

    async handleSessions(_command, _args, _context) {
        await this._showSessionPicker();
    }

    async _showSessionPicker() {
        try {
            const resp = await fetch(
                apiUrl(`/api/claude-code/sessions${this.forkIndex.cwd ? `?cwd=${encodeURIComponent(this.forkIndex.cwd)}` : ''}`),
            );

            if (!resp.ok) {
                throw new Error(`HTTP ${resp.status}`);
            }

            const sessions = await resp.json();

            if (sessions.length === 0) {
                this.showToast?.('No Claude Code sessions found.');
                return;
            }

            const lines = ['# Claude Code Sessions\n'];
            lines.push('| Session | First Prompt | Messages | Project |');
            lines.push('|---------|-------------|----------|---------|');

            for (const s of sessions.slice(0, 30)) {
                const shortId = s.session_id.slice(0, 8);
                const prompt = (s.first_prompt || '').replace(/[|<>]/g, '').slice(0, 60);
                lines.push(`| \`${shortId}\` | ${prompt} | ${s.message_count} | ${s.project_dir?.slice(0, 20) || ''} |`);
            }

            lines.push(`\nUse \`/cc-import <session-id>\` to import a session.`);

            const node = createNode(NodeType.NOTE, lines.join('\n'), {
                position: this.graph.autoPosition([]),
                width: 600,
            });
            this.graph.addNode(node);
            this.canvas.selectNode(node.id);
            this.saveSession();
        } catch (err) {
            console.error('[ClaudeCode] Sessions error:', err);
            this.showToast?.(`Failed to list sessions: ${err.message}`);
        }
    }

    // -- /cc-cwd [path] -----------------------------------------------------

    async handleCwd(_command, args, _context) {
        const path = args?.trim();

        if (!path) {
            const current = this.forkIndex.cwd || '(not set)';
            this.showToast?.(`Working directory: ${current}`);
            return;
        }

        this.forkIndex.cwd = path;
        this._saveIndex();
        this.showToast?.(`Working directory set to: ${path}`);
    }
}

// ---------------------------------------------------------------------------
// Self-registration
// ---------------------------------------------------------------------------

if (typeof window !== 'undefined') {
    const registerFeature = (app) => {
        if (app?.featureRegistry?._appContext) {
            app.featureRegistry
                .register({
                    id: 'claude-code',
                    feature: ClaudeCodeFeature,
                    slashCommands: [
                        { command: '/cc', handler: 'handleCC' },
                        { command: '/cc-import', handler: 'handleImport' },
                        { command: '/cc-import-all', handler: 'handleImportAll' },
                        { command: '/cc-sessions', handler: 'handleSessions' },
                        { command: '/cc-cwd', handler: 'handleCwd' },
                    ],
                    priority: 500,
                })
                .then(() => console.log('[ClaudeCode] Feature registered'))
                .catch((err) => console.error('[ClaudeCode] Registration failed:', err));
        }
    };

    if (window.app) {
        registerFeature(window.app);
    }

    window.addEventListener('app-plugin-system-ready', (event) => {
        registerFeature(event.detail.app);
    });
}
