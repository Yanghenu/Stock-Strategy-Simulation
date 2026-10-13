/** 将配置的分时刷新秒数转换为安全的浏览器计时器间隔。 */
export function intradayRefreshIntervalMs(seconds: number | undefined): number {
  return Math.max(2, seconds ?? 2) * 1000
}
