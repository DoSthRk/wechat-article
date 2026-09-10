"use strict";

// 微信草稿使用 data-src 懒加载；独立预览页需要补齐图片 src。
document.querySelectorAll(".wxbody img").forEach((image) => {
  image.referrerPolicy = "no-referrer";
  const source = image.getAttribute("data-src");
  if (!image.getAttribute("src") && /^https?:\/\//i.test(source || "")) {
    image.src = source;
  }
});
