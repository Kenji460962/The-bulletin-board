export default {
  async fetch(request, env, ctx) {
    
    const renderUrl = "https://onrender.com";
    
    const url = new URL(request.url);
    const targetUrl = renderUrl + url.pathname + url.search;
    
  
    const response = await fetch(targetUrl, {
      method: request.method,
      headers: request.headers,
      body: request.body
    });
    
    return response;
  }
}
