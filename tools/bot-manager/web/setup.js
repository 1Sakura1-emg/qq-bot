'use strict';
document.getElementById('copyPolicy').addEventListener('click',async () => {
  const toast=document.getElementById('setupToast');
  try { await navigator.clipboard.writeText(document.getElementById('policyText').textContent);toast.textContent='权限策略已复制'; }
  catch { toast.textContent='请手动选择并复制下方 JSON 策略。'; }
  toast.classList.remove('hidden');setTimeout(() => toast.classList.add('hidden'),3000);
});
