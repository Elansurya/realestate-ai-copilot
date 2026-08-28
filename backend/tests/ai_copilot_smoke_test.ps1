<#
.SYNOPSIS
    AI Copilot smoke test - verified route map only.

.DESCRIPTION
    Tests ONLY the 12 routes verified directly against the FastAPI source
    (app/api/v1/ai.py) and app/main.py's root-level /health route.
    No routes are guessed. No backend code is touched.

.NOTES
    Route corrections applied vs. the previous (404-prone) version:
      /api/v1/conversations        -> /api/v1/ai/conversations
      /api/v1/rag                  -> /api/v1/ai/rag/query
      /api/v1/prompt-templates     -> /api/v1/ai/prompts
      /api/v1/sql-ai               -> /api/v1/ai/sql/query
      /api/v1/analytics            -> /api/v1/ai/analytics/query
      /api/v1/health               -> /health
    No standalone embeddings route is tested (none exists).
#>

param(
    [string]$BaseUrl = "http://localhost:8000"
)

$ErrorActionPreference = "Stop"

$LoginUsername = "backend.audit.2026@test.com"
$LoginPassword = "AuditTest@2026!"

# --------------------------------------------------------------------------
# Result tracking
# --------------------------------------------------------------------------

$Results = New-Object System.Collections.Generic.List[object]

function Invoke-SmokeRequest {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Method,
        [Parameter(Mandatory)][string]$Url,
        [hashtable]$Headers = @{},
        [object]$Body = $null
    )

    $statusCode = 0
    $responseBody = $null
    $networkError = $null

    $invokeParams = @{
        Uri             = $Url
        Method          = $Method
        Headers         = $Headers
        UseBasicParsing = $true
    }

    if ($null -ne $Body) {
        $invokeParams["Body"] = ($Body | ConvertTo-Json -Depth 10)
        $invokeParams["ContentType"] = "application/json"
    }

    try {
        $response = Invoke-WebRequest @invokeParams
        $statusCode = [int]$response.StatusCode
        $responseBody = $response.Content
    }
    catch {
        $ex = $_.Exception
        if ($ex.Response) {
            try {
                $statusCode = [int]$ex.Response.StatusCode
            }
            catch {
                $statusCode = 0
            }

            # Read body across both PowerShell 5.1 (System.Net.HttpWebResponse)
            # and PowerShell 6+/7 (System.Net.Http.HttpResponseMessage) error shapes.
            try {
                if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
                    $responseBody = $_.ErrorDetails.Message
                }
                elseif ($ex.Response.GetResponseStream) {
                    $stream = $ex.Response.GetResponseStream()
                    $reader = New-Object System.IO.StreamReader($stream)
                    $responseBody = $reader.ReadToEnd()
                    $reader.Close()
                }
            }
            catch {
                $responseBody = "<unable to read response body: $($_.Exception.Message)>"
            }
        }
        else {
            # No HTTP response at all - connection failure, DNS failure, etc.
            $networkError = $ex.Message
        }
    }

    $result = Classify-Status -StatusCode $statusCode -NetworkError $networkError

    $record = [pscustomobject]@{
        Name     = $Name
        Method   = $Method
        Url      = $Url
        Status   = $statusCode
        Result   = $result
        Body     = $responseBody
        NetError = $networkError
    }

    $Results.Add($record) | Out-Null

    Write-Host ""
    Write-Host "METHOD : $Method"
    Write-Host "URL    : $Url"
    if ($networkError) {
        Write-Host "STATUS : (no response - network error)"
    }
    else {
        Write-Host "STATUS : $statusCode"
    }
    Write-Host "RESULT : $result"

    if ($result -ne "PASS") {
        if ($networkError) {
            Write-Host "DETAIL : $networkError" -ForegroundColor Yellow
        }
        elseif ($responseBody) {
            Write-Host "BODY   : $responseBody" -ForegroundColor Yellow
        }
    }

    return $record
}

function Classify-Status {
    param(
        [int]$StatusCode,
        [string]$NetworkError
    )

    if ($NetworkError) { return "NETWORK ERROR" }

    switch ($StatusCode) {
        200 { return "PASS" }
        201 { return "PASS" }
        204 { return "PASS" }
        400 { return "BAD REQUEST" }
        401 { return "AUTH ERROR" }
        403 { return "FORBIDDEN" }
        404 { return "ROUTE/RESOURCE NOT FOUND" }
        405 { return "METHOD NOT ALLOWED" }
        422 { return "VALIDATION ERROR" }
        default {
            if ($StatusCode -ge 500) { return "SERVER ERROR" }
            return "UNEXPECTED STATUS ($StatusCode)"
        }
    }
}

# --------------------------------------------------------------------------
# 1. Login (fail immediately if this fails)
# --------------------------------------------------------------------------

Write-Host "================================================================"
Write-Host " AI COPILOT SMOKE TEST"
Write-Host " Base URL: $BaseUrl"
Write-Host "================================================================"

$loginUrl = "$BaseUrl/api/v1/auth/login"

Write-Host ""
Write-Host "--- LOGIN ---"

try {
    $loginResponse = Invoke-RestMethod `
        -Uri $loginUrl `
        -Method POST `
        -ContentType "application/x-www-form-urlencoded" `
        -Body @{
            username = $LoginUsername
            password = $LoginPassword
        } `
        -ErrorAction Stop
}
catch {
    Write-Host ""
    Write-Host "FATAL: Login request failed. Cannot continue smoke test." -ForegroundColor Red
    $ex = $_.Exception
    if ($ex.Response) {
        try { $failedStatus = [int]$ex.Response.StatusCode } catch { $failedStatus = 0 }
        Write-Host "STATUS : $failedStatus" -ForegroundColor Red
    }
    Write-Host "ERROR  : $($ex.Message)" -ForegroundColor Red
    exit 1
}

if (-not $loginResponse -or -not $loginResponse.access_token) {
    Write-Host ""
    Write-Host "FATAL: Login succeeded at the HTTP level but no access_token was returned. Cannot continue." -ForegroundColor Red
    exit 1
}

$accessToken = $loginResponse.access_token
$tokenLength = $accessToken.Length

Write-Host "METHOD : POST"
Write-Host "URL    : $loginUrl"
Write-Host "STATUS : 200/201 (token received)"
Write-Host "RESULT : PASS"
Write-Host "ACCESS TOKEN LENGTH : $tokenLength (token value not printed)"

$authHeaders = @{
    "Authorization" = "Bearer $accessToken"
}

# --------------------------------------------------------------------------
# 2. Route tests (verified route map only)
# --------------------------------------------------------------------------

Write-Host ""
Write-Host "--- ROUTE TESTS ---"

# 1. POST /api/v1/ai/conversations
Invoke-SmokeRequest -Name "Create conversation" -Method "POST" `
    -Url "$BaseUrl/api/v1/ai/conversations" `
    -Headers $authHeaders `
    -Body @{
        title  = "Smoke Test Conversation"
        module = "chat"
    } | Out-Null

# 2. GET /api/v1/ai/conversations
Invoke-SmokeRequest -Name "List conversations" -Method "GET" `
    -Url "$BaseUrl/api/v1/ai/conversations" `
    -Headers $authHeaders | Out-Null

# 3. GET /api/v1/ai/documents
Invoke-SmokeRequest -Name "List documents" -Method "GET" `
    -Url "$BaseUrl/api/v1/ai/documents" `
    -Headers $authHeaders | Out-Null

# 4. GET /api/v1/ai/prompts
Invoke-SmokeRequest -Name "List prompts" -Method "GET" `
    -Url "$BaseUrl/api/v1/ai/prompts" `
    -Headers $authHeaders | Out-Null

# 5. GET /api/v1/ai/usage-logs (ADMIN)
Invoke-SmokeRequest -Name "List usage logs (admin)" -Method "GET" `
    -Url "$BaseUrl/api/v1/ai/usage-logs" `
    -Headers $authHeaders | Out-Null

# 6. GET /api/v1/ai/usage-logs/summary (ADMIN/MANAGER)
Invoke-SmokeRequest -Name "Usage logs summary (admin/manager)" -Method "GET" `
    -Url "$BaseUrl/api/v1/ai/usage-logs/summary" `
    -Headers $authHeaders | Out-Null

# 7. POST /api/v1/ai/rag/query
Invoke-SmokeRequest -Name "RAG query" -Method "POST" `
    -Url "$BaseUrl/api/v1/ai/rag/query" `
    -Headers $authHeaders `
    -Body @{
        question = "What properties are available?"
    } | Out-Null

# 8. POST /api/v1/ai/sql/query (ADMIN/MANAGER)
Invoke-SmokeRequest -Name "SQL AI query (admin/manager)" -Method "POST" `
    -Url "$BaseUrl/api/v1/ai/sql/query" `
    -Headers $authHeaders `
    -Body @{
        question = "How many leads are currently in the system?"
        execute  = $false
    } | Out-Null

# 9. POST /api/v1/ai/analytics/query
Invoke-SmokeRequest -Name "Analytics AI query" -Method "POST" `
    -Url "$BaseUrl/api/v1/ai/analytics/query" `
    -Headers $authHeaders `
    -Body @{
        question = "Summarize the dataset"
        dataset  = @(
            @{ id = 1; value = 100 }
        )
    } | Out-Null

# 10. GET /health (no auth)
Invoke-SmokeRequest -Name "Health check" -Method "GET" `
    -Url "$BaseUrl/health" `
    -Headers @{} | Out-Null

# 11. POST /api/v1/ai/prompts (ADMIN/MANAGER) - creates a smoke-test resource.
#     Per instructions, DELETE is not permitted, so this record is left in
#     place and clearly labeled as a smoke-test creation.
$promptRecord = Invoke-SmokeRequest -Name "Create prompt (admin/manager) [SMOKE-TEST CREATION - not cleaned up, DELETE disallowed]" -Method "POST" `
    -Url "$BaseUrl/api/v1/ai/prompts" `
    -Headers $authHeaders `
    -Body @{
        name          = "Smoke Test Prompt"
        template_text = "Hello {{name}}"
    }

# 12. POST /api/v1/ai/prompts/{id}/render
#     Uses the id of the prompt created in test 11. This test only runs if
#     that creation actually returned an id. It deliberately does NOT fall
#     back to a random/fake GUID, since that would manufacture an artificial
#     404 rather than reporting a real backend problem.
$promptId = $null
if ($promptRecord -and $promptRecord.Body) {
    try {
        $parsedPrompt = $promptRecord.Body | ConvertFrom-Json
        if ($parsedPrompt.id) {
            $promptId = $parsedPrompt.id
        }
    }
    catch {
        $promptId = $null
    }
}

Write-Host ""
if ($promptId) {
    Invoke-SmokeRequest -Name "Render prompt" -Method "POST" `
        -Url "$BaseUrl/api/v1/ai/prompts/$promptId/render" `
        -Headers $authHeaders `
        -Body @{
            variables = @{
                name = "Test"
            }
        } | Out-Null
}
else {
    $skipReason = "Prompt creation (test 11) did not return an id, so there is no real prompt to render. Skipping rather than testing against a fake/random id."

    Write-Host "METHOD : POST"
    Write-Host "URL    : $BaseUrl/api/v1/ai/prompts/{id}/render"
    Write-Host "STATUS : (not sent)"
    Write-Host "RESULT : SKIPPED"
    Write-Host "REASON : $skipReason" -ForegroundColor Yellow

    $Results.Add([pscustomobject]@{
        Name     = "Render prompt"
        Method   = "POST"
        Url      = "$BaseUrl/api/v1/ai/prompts/{id}/render"
        Status   = $null
        Result   = "SKIPPED"
        Body     = $skipReason
        NetError = $null
    }) | Out-Null
}

# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------

$passCount     = ($Results | Where-Object { $_.Result -eq "PASS" }).Count
$fourXXCount   = ($Results | Where-Object { $null -ne $_.Status -and $_.Status -ge 400 -and $_.Status -lt 500 }).Count
$notFoundCount = ($Results | Where-Object { $null -ne $_.Status -and $_.Status -eq 404 }).Count
$validationCount = ($Results | Where-Object { $null -ne $_.Status -and $_.Status -eq 422 }).Count
$serverErrorCount = ($Results | Where-Object { $null -ne $_.Status -and $_.Status -ge 500 }).Count
$skippedCount  = ($Results | Where-Object { $_.Result -eq "SKIPPED" }).Count
$totalCount    = $Results.Count

Write-Host ""
Write-Host "================================================================"
Write-Host " SUMMARY"
Write-Host "================================================================"
Write-Host "PASS                : $passCount"
Write-Host "4xx CLIENT ERRORS   : $fourXXCount"
Write-Host "404 ROUTE ERRORS    : $notFoundCount"
Write-Host "422 VALIDATION ERRORS : $validationCount"
Write-Host "500 SERVER ERRORS   : $serverErrorCount"
Write-Host "SKIPPED             : $skippedCount"
Write-Host "TOTAL TESTS         : $totalCount"
Write-Host "================================================================"

if ($notFoundCount -gt 0 -or $serverErrorCount -gt 0) {
    exit 1
}
else {
    exit 0
}