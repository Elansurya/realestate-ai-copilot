$ErrorActionPreference = "Continue"

$base = "http://127.0.0.1:8000/api/v1"

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " UNIVERSAL BACKEND READ-ONLY SMOKE TEST" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# -----------------------------
# LOGIN
# -----------------------------
Write-Host "`n--- LOGIN ---" -ForegroundColor Yellow

try {
    $login = Invoke-RestMethod `
        -Uri "$base/auth/login" `
        -Method POST `
        -ContentType "application/x-www-form-urlencoded" `
        -Body @{
            username = "backend.audit.2026@test.com"
            password = "AuditTest@2026!"
        } `
        -ErrorAction Stop

    $token = $login.access_token

    if (-not $token) {
        Write-Host "LOGIN FAILED - NO TOKEN" -ForegroundColor Red
        exit 1
    }

    Write-Host "LOGIN : 200 PASS" -ForegroundColor Green
    Write-Host "TOKEN : $($token.Length) chars (value hidden)" -ForegroundColor DarkGray
}
catch {
    Write-Host "LOGIN FAILED" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}

$headers = @{
    Authorization = "Bearer $token"
}

# -----------------------------
# OPENAPI
# -----------------------------
Write-Host "`n--- OPENAPI DISCOVERY ---" -ForegroundColor Yellow

try {
    $openapi = Invoke-RestMethod `
        -Uri "$base/../openapi.json" `
        -Method GET `
        -ErrorAction Stop

    Write-Host "OPENAPI : PASS" -ForegroundColor Green
}
catch {
    try {
        $openapi = Invoke-RestMethod `
            -Uri "http://127.0.0.1:8000/openapi.json" `
            -Method GET `
            -ErrorAction Stop

        Write-Host "OPENAPI : PASS" -ForegroundColor Green
    }
    catch {
        Write-Host "OPENAPI : FAILED" -ForegroundColor Red
        exit 1
    }
}

# -----------------------------
# SAFE GET ROUTES
# -----------------------------
# Only fixed-path GET endpoints.
# No {id}, no POST, no PATCH, no DELETE.
# -----------------------------

$skipPatterns = @(
    "/{",
    "/search",
    "/export",
    "/download",
    "/preview",
    "/thumbnail",
    "/restore",
    "/cleanup",
    "/statistics",
    "/history",
    "/timeline",
    "/recent",
    "/failed",
    "/critical",
    "/overdue",
    "/reminders",
    "/status",
    "/health-check",
    "/test-connection",
    "/delivery-status",
    "/logs",
    "/read-status",
    "/unread/count",
    "/queue/",
    "/templates/",
    "/bulk",
    "/approvals/",
    "/steps/"
)

$routes = @()

foreach ($property in $openapi.paths.PSObject.Properties) {

    $path = $property.Name
    $methods = $property.Value.PSObject.Properties.Name

    if ($methods -contains "get") {

        $skip = $false

        foreach ($pattern in $skipPatterns) {
            if ($path -like "*$pattern*") {
                $skip = $true
                break
            }
        }

        if (-not $skip) {
            $routes += $path
        }
    }
}

$routes = $routes | Sort-Object -Unique

Write-Host "SAFE GET ROUTES FOUND : $($routes.Count)" -ForegroundColor Cyan

# -----------------------------
# RESULTS
# -----------------------------

$results = @()

foreach ($path in $routes) {

    $url = "http://127.0.0.1:8000$path"

    try {

        $response = Invoke-WebRequest `
            -Uri $url `
            -Method GET `
            -Headers $headers `
            -UseBasicParsing `
            -ErrorAction Stop

        $code = [int]$response.StatusCode

        if ($code -ge 200 -and $code -lt 300) {
            $result = "PASS"
            $color = "Green"
        }
        elseif ($code -ge 400 -and $code -lt 500) {
            $result = "CLIENT ERROR"
            $color = "Yellow"
        }
        elseif ($code -ge 500) {
            $result = "SERVER ERROR"
            $color = "Red"
        }
        else {
            $result = "CHECK"
            $color = "Yellow"
        }

        Write-Host ("{0,-65} {1,4} {2}" -f $path,$code,$result) -ForegroundColor $color

    }
    catch {

        $code = $null
        $body = ""

        if ($_.Exception.Response) {

            try {
                $code = [int]$_.Exception.Response.StatusCode
            }
            catch {}

            try {
                $reader = New-Object System.IO.StreamReader(
                    $_.Exception.Response.GetResponseStream()
                )
                $body = $reader.ReadToEnd()
                $reader.Dispose()
            }
            catch {}
        }

        if ($code -eq 401) {
            $result = "AUTH ERROR"
            $color = "Red"
        }
        elseif ($code -eq 403) {
            $result = "FORBIDDEN"
            $color = "Yellow"
        }
        elseif ($code -eq 404) {
            $result = "ROUTE ERROR"
            $color = "Yellow"
        }
        elseif ($code -eq 422) {
            $result = "VALIDATION ERROR"
            $color = "Yellow"
        }
        elseif ($code -ge 500) {
            $result = "SERVER ERROR"
            $color = "Red"
        }
        else {
            $result = "FAILED"
            $color = "Red"
        }

        Write-Host ("{0,-65} {1,4} {2}" -f $path,$code,$result) -ForegroundColor $color

        if ($code -ge 500) {
            if ($body) {
                Write-Host "    BODY: $body" -ForegroundColor DarkRed
            }
        }
    }

    $results += [PSCustomObject]@{
        Route = $path
        StatusCode = $code
        Result = $result
    }
}

# -----------------------------
# ROOT HEALTH
# -----------------------------

Write-Host "`n--- ROOT HEALTH ---" -ForegroundColor Yellow

try {

    $health = Invoke-WebRequest `
        -Uri "http://127.0.0.1:8000/health" `
        -Method GET `
        -UseBasicParsing `
        -ErrorAction Stop

    Write-Host "/health : $($health.StatusCode) PASS" -ForegroundColor Green

    $results += [PSCustomObject]@{
        Route = "/health"
        StatusCode = [int]$health.StatusCode
        Result = "PASS"
    }
}
catch {

    Write-Host "/health : FAILED" -ForegroundColor Red

    $results += [PSCustomObject]@{
        Route = "/health"
        StatusCode = $null
        Result = "FAILED"
    }
}

# -----------------------------
# SUMMARY
# -----------------------------

$pass = @($results | Where-Object {
    $_.StatusCode -ge 200 -and $_.StatusCode -lt 300
}).Count

$client = @($results | Where-Object {
    $_.StatusCode -ge 400 -and $_.StatusCode -lt 500
}).Count

$server = @($results | Where-Object {
    $_.StatusCode -ge 500
}).Count

$auth = @($results | Where-Object {
    $_.StatusCode -eq 401 -or $_.StatusCode -eq 403
}).Count

$notFound = @($results | Where-Object {
    $_.StatusCode -eq 404
}).Count

$validation = @($results | Where-Object {
    $_.StatusCode -eq 422
}).Count

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " UNIVERSAL TEST SUMMARY" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

Write-Host "PASS                 : $pass" -ForegroundColor Green
Write-Host "CLIENT ERRORS        : $client" -ForegroundColor Yellow
Write-Host "AUTH ERRORS          : $auth" -ForegroundColor Yellow
Write-Host "404 ROUTE ERRORS     : $notFound" -ForegroundColor Yellow
Write-Host "422 VALIDATION       : $validation" -ForegroundColor Yellow
Write-Host "500 SERVER ERRORS    : $server" -ForegroundColor Red
Write-Host "TOTAL TESTED         : $($results.Count)" -ForegroundColor Cyan

Write-Host "`n--- SERVER ERROR ROUTES ---" -ForegroundColor Red

$serverErrors = @($results | Where-Object {
    $_.StatusCode -ge 500
})

if ($serverErrors.Count -eq 0) {
    Write-Host "NONE" -ForegroundColor Green
}
else {
    $serverErrors | Format-Table -AutoSize
}

Write-Host "`n==================================================" -ForegroundColor Cyan
Write-Host " READ-ONLY TEST COMPLETE" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
