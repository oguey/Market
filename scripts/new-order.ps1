param([int]$CustomerId = 1, [int]$ProductId = 1, [int]$Quantity = 1, [int]$Count = 1,
      [string]$Email = "admin@market.test", [string]$Base = "http://localhost:3000")
$secure = Read-Host "Mot de passe de $Email" -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
try { $login = Invoke-RestMethod -Method Post -Uri "$Base/api/auth/login" -ContentType "application/json" -Body (@{ email = $Email; password = $plain } | ConvertTo-Json) }
catch { Write-Host "Connexion refusee : $($_.Exception.Message)" -ForegroundColor Red; exit 1 }
$token = $login.access_token
$body = @{ customer_id = $CustomerId; items = @(@{ product_id = $ProductId; quantity = $Quantity }) } | ConvertTo-Json -Depth 5
for ($i = 1; $i -le $Count; $i++) {
  $key = "cli-" + (Get-Date -Format "yyyyMMddHHmmssfff") + "-$i"
  try {
    $o = Invoke-RestMethod -Method Post -Uri "$Base/api/orders/orders" -Headers @{ Authorization = "Bearer $token"; "Idempotency-Key" = $key } -ContentType "application/json" -Body $body
    Write-Host ("Commande #{0} : statut={1} total={2:N2} EUR" -f $o.id, $o.status, ($o.total_cents / 100))
  } catch { Write-Host "Echec : $($_.Exception.Message)" -ForegroundColor Red }
}
