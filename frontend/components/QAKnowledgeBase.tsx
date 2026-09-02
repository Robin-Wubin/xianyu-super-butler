import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  BookMarked,
  Check,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
  X,
} from 'lucide-react';
import { AccountDetail, Item } from '../types';
import {
  createQAPair,
  deleteQAPair,
  getAccountDetails,
  getItems,
  getQAPairs,
  QAPair,
  toggleQAPair,
  updateQAPair,
} from '../services/api';
import { confirmAction, notify } from '../services/feedback';
import { EmptyState, SectionHeader } from './ui';

type EditState = {
  id: number | null;          // null=新增
  question: string;
  answer: string;
  item_id: string;            // ''=通用
};

const emptyEdit: EditState = { id: null, question: '', answer: '', item_id: '' };

const QAKnowledgeBase: React.FC<{ accountId?: string }> = ({ accountId }) => {
  const [accounts, setAccounts] = useState<AccountDetail[]>([]);
  const [items, setItems] = useState<Item[]>([]);
  const [selectedAccount, setSelectedAccount] = useState(accountId || '');
  const [pairs, setPairs] = useState<QAPair[]>([]);
  const [loading, setLoading] = useState(false);
  const [filterScope, setFilterScope] = useState<'all' | 'item' | 'global'>('all');
  const [edit, setEdit] = useState<EditState | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (accountId) {
      setSelectedAccount(accountId);
      return;
    }
    getAccountDetails()
      .then((data) => {
        setAccounts(data);
        setSelectedAccount((current) => current || data[0]?.id || '');
      })
      .catch((error) => notify(`加载账号失败：${(error as Error).message}`, 'error'));
  }, [accountId]);

  useEffect(() => {
    if (!selectedAccount) return;
    getItems()
      .then(setItems)
      .catch(() => setItems([]));
  }, [selectedAccount]);

  const load = useCallback(async () => {
    if (!selectedAccount) return;
    setLoading(true);
    try {
      setPairs(await getQAPairs(selectedAccount));
    } catch (error) {
      notify(`加载问答库失败：${(error as Error).message}`, 'error');
    } finally {
      setLoading(false);
    }
  }, [selectedAccount]);

  useEffect(() => { void load(); }, [load]);

  const itemTitle = useMemo(() => {
    const map = new Map(items.map((item) => [`${item.cookie_id}:${item.item_id}`, item]));
    return (itemId: string) => {
      const found = map.get(`${selectedAccount}:${itemId}`);
      return found?.item_title || itemId;
    };
  }, [items, selectedAccount]);

  const visiblePairs = useMemo(() => {
    if (filterScope === 'all') return pairs;
    if (filterScope === 'global') return pairs.filter((pair) => !pair.item_id);
    return pairs.filter((pair) => pair.item_id);
  }, [pairs, filterScope]);

  const handleSave = async () => {
    if (!edit) return;
    if (!edit.question.trim() || !edit.answer.trim()) {
      notify('问题和回答都不能为空', 'warning');
      return;
    }
    setSaving(true);
    try {
      if (edit.id) {
        await updateQAPair(selectedAccount, edit.id, {
          question: edit.question, answer: edit.answer, item_id: edit.item_id,
        });
      } else {
        await createQAPair(selectedAccount, {
          question: edit.question, answer: edit.answer, item_id: edit.item_id, source: 'manual',
        });
      }
      notify(edit.id ? '问答已更新' : '问答已添加', 'success');
      setEdit(null);
      void load();
    } catch (error) {
      notify(`保存失败：${(error as Error).message}`, 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (pair: QAPair) => {
    const ok = await confirmAction(`删除这条问答？\n\n问：${pair.question.slice(0, 50)}…`, '删除后不可恢复');
    if (!ok) return;
    try {
      await deleteQAPair(selectedAccount, pair.id);
      notify('问答已删除', 'success');
      void load();
    } catch (error) {
      notify(`删除失败：${(error as Error).message}`, 'error');
    }
  };

  const handleToggle = async (pair: QAPair) => {
    try {
      await toggleQAPair(selectedAccount, pair.id);
      void load();
    } catch (error) {
      notify(`操作失败：${(error as Error).message}`, 'error');
    }
  };

  return (
    <section className="section-panel p-5">
      <SectionHeader
        icon={BookMarked}
        title="问答库"
        description="从消息中心节选或手动添加的问答对。AI 回复时优先按这里的口径回答；商品级问答优先于通用问答。"
        actions={(
          <div className="flex flex-wrap items-center gap-2">
            {!accountId && (
              <select
                value={selectedAccount}
                onChange={(event) => setSelectedAccount(event.target.value)}
                className="rounded-md border border-[var(--border)] bg-[var(--surface-strong)] px-2.5 py-1.5 text-xs"
              >
                {accounts.map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.nickname || account.remark || `账号 ${account.id.slice(0, 8)}`}
                  </option>
                ))}
              </select>
            )}
            <button
              type="button"
              onClick={() => void load()}
              className="rounded-md p-2 hover:bg-[var(--surface-hover)]"
              title="刷新"
            >
              <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            </button>
            <button
              type="button"
              onClick={() => setEdit({ ...emptyEdit })}
              className="inline-flex items-center gap-1.5 rounded-md bg-blue-600 px-3 py-1.5 text-xs font-bold text-white hover:bg-blue-700"
            >
              <Plus className="h-3.5 w-3.5" /> 手动添加
            </button>
          </div>
        )}
      />

      <div className="mb-3 flex items-center gap-2 text-xs">
        {([
          ['all', `全部 (${pairs.length})`],
          ['item', `商品专属 (${pairs.filter((p) => p.item_id).length})`],
          ['global', `通用 (${pairs.filter((p) => !p.item_id).length})`],
        ] as const).map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => setFilterScope(key)}
            className={`rounded-full px-3 py-1 font-bold ${
              filterScope === key
                ? 'bg-[var(--brand)] text-[var(--brand-ink)]'
                : 'bg-[var(--surface-strong)] text-[var(--text-muted)] hover:bg-[var(--surface-hover)]'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {edit && (
        <div className="mb-4 rounded-md border border-blue-200 bg-blue-50/50 p-4">
          <div className="mb-2 flex items-center justify-between">
            <p className="text-sm font-bold">{edit.id ? '编辑问答' : '新增问答'}</p>
            <button type="button" onClick={() => setEdit(null)} className="rounded p-1 hover:bg-[var(--surface-hover)]" aria-label="取消">
              <X className="h-4 w-4" />
            </button>
          </div>
          <div className="space-y-2">
            <textarea
              value={edit.question}
              onChange={(event) => setEdit({ ...edit, question: event.target.value })}
              placeholder="问题（买家会问的话），如：这个支持7天无理由吗？"
              rows={2}
              className="w-full rounded-md border border-[var(--border)] bg-[var(--surface-strong)] px-3 py-2 text-sm"
            />
            <textarea
              value={edit.answer}
              onChange={(event) => setEdit({ ...edit, answer: event.target.value })}
              placeholder="回答（按你的口径），如：支持，收到7天内不影响二次销售可退。"
              rows={3}
              className="w-full rounded-md border border-[var(--border)] bg-[var(--surface-strong)] px-3 py-2 text-sm"
            />
            <select
              value={edit.item_id}
              onChange={(event) => setEdit({ ...edit, item_id: event.target.value })}
              className="w-full rounded-md border border-[var(--border)] bg-[var(--surface-strong)] px-3 py-2 text-sm"
            >
              <option value="">通用（所有商品生效）</option>
              {items
                .filter((item) => item.cookie_id === selectedAccount)
                .map((item) => (
                  <option key={item.item_id} value={item.item_id}>
                    {item.item_title || item.item_id}
                  </option>
                ))}
            </select>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setEdit(null)}
                className="rounded-md px-3 py-1.5 text-xs font-bold text-[var(--text-muted)] hover:bg-[var(--surface-hover)]"
              >
                取消
              </button>
              <button
                type="button"
                disabled={saving}
                onClick={() => void handleSave()}
                className="inline-flex items-center gap-1.5 rounded-md bg-blue-600 px-4 py-1.5 text-xs font-bold text-white hover:bg-blue-700 disabled:opacity-50"
              >
                {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                保存
              </button>
            </div>
          </div>
        </div>
      )}

      {loading && pairs.length === 0 ? (
        <div className="flex justify-center py-10">
          <Loader2 className="h-6 w-6 animate-spin text-[var(--brand)]" />
        </div>
      ) : visiblePairs.length === 0 ? (
        <EmptyState
          icon={BookMarked}
          title="还没有问答"
          description="在消息中心打开对话，点「节选问答」从真实聊天中收录；或点上方「手动添加」。"
        />
      ) : (
        <div className="space-y-2">
          {visiblePairs.map((pair) => (
            <div
              key={pair.id}
              className={`rounded-md border p-3 ${
                pair.enabled ? 'border-[var(--border)]' : 'border-dashed border-[var(--border)] opacity-60'
              }`}
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <p className="mb-1 text-xs font-bold text-blue-600">
                    问{pair.item_id ? ` · ${itemTitle(pair.item_id)}` : ' · 通用'}
                  </p>
                  <p className="whitespace-pre-wrap break-words text-sm text-[var(--text)]">{pair.question}</p>
                  <p className="mt-2 whitespace-pre-wrap break-words text-sm text-[var(--text)]">
                    <span className="mr-1 text-xs font-bold text-emerald-600">答</span>
                    {pair.answer}
                  </p>
                  <p className="mt-1.5 text-[11px] text-[var(--text-soft)]">
                    {pair.source === 'chat' ? '对话节选' : '手动添加'} · {pair.updated_at?.slice(0, 16)}
                  </p>
                </div>
                <div className="flex shrink-0 flex-col gap-1">
                  <button
                    type="button"
                    onClick={() => void handleToggle(pair)}
                    className={`rounded p-1.5 hover:bg-[var(--surface-hover)] ${pair.enabled ? 'text-emerald-600' : 'text-[var(--text-soft)]'}`}
                    title={pair.enabled ? '停用' : '启用'}
                  >
                    <Check className="h-4 w-4" />
                  </button>
                  <button
                    type="button"
                    onClick={() => setEdit({ id: pair.id, question: pair.question, answer: pair.answer, item_id: pair.item_id || '' })}
                    className="rounded p-1.5 text-[var(--text-muted)] hover:bg-[var(--surface-hover)]"
                    title="编辑"
                  >
                    <Pencil className="h-4 w-4" />
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleDelete(pair)}
                    className="rounded p-1.5 text-red-500 hover:bg-red-50"
                    title="删除"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
};

export default QAKnowledgeBase;
