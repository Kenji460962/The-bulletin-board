export default {
  async fetch(request, env, ctx) {
    // 🟢 あなたの今のRenderのURLに書き換えてください（例: https://onrender.com）
    const renderUrl = "https://onrender.com";
    
    const url = new URL(request.url);
    const targetUrl = renderUrl + url.pathname + url.search;
    
    // ユーザーからのアクセスをそのままRenderに転送し、結果を爆速で返します
    const response = await fetch(targetUrl, {
      method: request.method,
      headers: request.headers,
      body: request.body
    });
    
    return response;
  }
}
