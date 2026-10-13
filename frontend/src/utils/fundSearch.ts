/** 空关键词不触发全市场下载，避免上游慢时阻塞基金页面初始化。 */
export function fundSearchTerm(query: string): string | null {
  return query.trim() || null
}
