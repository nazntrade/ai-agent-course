param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Destination
)
$text = [System.IO.File]::ReadAllText($Source)
$text = $text.Replace("`r`n", "`n").Replace("`r", "`n")
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($Destination, $text, $utf8)
$check = [System.IO.File]::ReadAllText($Destination)
if ($check.Contains("`r")) { throw "CR character remained after normalization" }
