# Ajoute ADR-013 a docs/ADR.md (ligne d'index + section). A lancer depuis la racine du projet.
$adr = "docs/ADR.md"
$sec = Get-Content "docs/ADR-013.md" -Raw -Encoding UTF8
$txt = Get-Content $adr -Raw -Encoding UTF8
if ($txt -match "## ADR-013") { Write-Host "ADR-013 deja present"; exit 0 }
$lines = [System.Collections.Generic.List[string]](Get-Content $adr -Encoding UTF8)
$last = -1
for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match '^\| \[?0\d\d') { $last = $i } }
if ($last -lt 0) { Write-Host "Index introuvable : ajoute la ligne a la main"; exit 1 }
$lines.Insert($last + 1, "| [013](#adr-013--distributed-tracing-with-opentelemetry-and-jaeger) | Distributed tracing: OpenTelemetry + Jaeger | accepted |")
Set-Content $adr -Value $lines -Encoding UTF8
Add-Content $adr -Value $sec -Encoding UTF8
Remove-Item "docs/ADR-013.md"
Write-Host "ADR-013 ajoute."
