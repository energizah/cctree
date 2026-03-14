/**
 * Claude Code Plugin for Canvas Chat
 *
 * Bridges Canvas Chat's visual canvas to the Claude Code CLI backend.
 * Uses fork-per-message to enable branching from any historical node.
 *
 * Slash commands:
 *   /cc <prompt>          — send a message via Claude Code CLI
 *   /cc-import [session]  — import a JSONL session onto the canvas
 *   /cc-sessions          — list available Claude Code sessions
 *   /cc-cwd [path]        — set/show the working directory for Claude Code
 */

import { FeaturePlugin } from '/static/js/feature-plugin.js';
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
// ClaudeCodeFeature
// ---------------------------------------------------------------------------

class ClaudeCodeFeature extends FeaturePlugin {
    constructor(context) {
        super(context);
        this.forkIndex = new ForkIndex();
        this._pendingForks = new Map(); // nodeId -> Promise
        this._toolStatus = new Map();   // nodeId -> current tool status element
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
        `);

        this._addModeIndicator();

        console.log('[ClaudeCode] Plugin loaded');
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
        this.canvas.renderNode(humanNode);

        // Create edges from parents
        for (const parentId of parentIds) {
            const edge = createEdge(
                parentId,
                humanNode.id,
                parentIds.length > 1 ? EdgeType.MERGE : EdgeType.REPLY,
            );
            this.graph.addEdge(edge);
            this.canvas.renderEdge(edge);
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
        this.canvas.renderNode(aiNode);

        const aiEdge = createEdge(humanNode.id, aiNode.id, EdgeType.REPLY);
        this.graph.addEdge(aiEdge);
        this.canvas.renderEdge(aiEdge);
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
                    body: JSON.stringify({ session_id: sessionId }),
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
                this.canvas.renderNode(node);

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
                this.canvas.renderEdge(edge);
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
            this.canvas.renderNode(node);
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
