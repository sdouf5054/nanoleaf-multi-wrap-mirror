/*
 * fast_capture.cpp - DXGI Desktop Duplication + CPU subsample DLL
 *
 * Build:
 *   cl /LD /O2 /EHsc fast_capture.cpp /link d3d11.lib dxgi.lib /OUT:fast_capture.dll
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <string.h>

/* ---- globals -------------------------------------------------- */

static ID3D11Device*           g_device      = NULL;
static ID3D11DeviceContext*    g_context     = NULL;
static IDXGIOutputDuplication* g_duplication = NULL;
static ID3D11Texture2D*        g_staging     = NULL;

static int  g_outW        = 64;
static int  g_outH        = 32;
static int  g_screenW     = 0;
static int  g_screenH     = 0;
static int  g_stagingW    = 0;
static int  g_stagingH    = 0;
static int  g_monitorIdx  = 0;
static BOOL g_initialized = FALSE;

#define SAFE_RELEASE(p) do { if (p) { (p)->Release(); (p) = NULL; } } while(0)
#define EXPORT extern "C" __declspec(dllexport)

/* ---- internal: create staging texture ------------------------- */

static HRESULT ensure_staging(void)
{
    if (g_staging && g_stagingW == g_screenW && g_stagingH == g_screenH)
        return S_OK;

    SAFE_RELEASE(g_staging);

    D3D11_TEXTURE2D_DESC desc;
    memset(&desc, 0, sizeof(desc));
    desc.Width              = (UINT)g_screenW;
    desc.Height             = (UINT)g_screenH;
    desc.MipLevels          = 1;
    desc.ArraySize          = 1;
    desc.Format             = DXGI_FORMAT_B8G8R8A8_UNORM;
    desc.SampleDesc.Count   = 1;
    desc.SampleDesc.Quality = 0;
    desc.Usage              = D3D11_USAGE_STAGING;
    desc.CPUAccessFlags     = D3D11_CPU_ACCESS_READ;
    desc.BindFlags          = 0;
    desc.MiscFlags          = 0;

    HRESULT hr = g_device->CreateTexture2D(&desc, NULL, &g_staging);
    if (SUCCEEDED(hr)) {
        g_stagingW = g_screenW;
        g_stagingH = g_screenH;
    }
    return hr;
}

/* ---- internal: acquire duplication ---------------------------- */

static HRESULT acquire_duplication(void)
{
    SAFE_RELEASE(g_duplication);

    IDXGIDevice* dxgiDev = NULL;
    HRESULT hr = g_device->QueryInterface(__uuidof(IDXGIDevice), (void**)&dxgiDev);
    if (FAILED(hr)) return hr;

    IDXGIAdapter* adapter = NULL;
    hr = dxgiDev->GetAdapter(&adapter);
    SAFE_RELEASE(dxgiDev);
    if (FAILED(hr)) return hr;

    IDXGIOutput* output = NULL;
    hr = adapter->EnumOutputs((UINT)g_monitorIdx, &output);
    SAFE_RELEASE(adapter);
    if (FAILED(hr)) return hr;

    DXGI_OUTPUT_DESC odesc;
    output->GetDesc(&odesc);
    g_screenW = odesc.DesktopCoordinates.right  - odesc.DesktopCoordinates.left;
    g_screenH = odesc.DesktopCoordinates.bottom - odesc.DesktopCoordinates.top;

    IDXGIOutput1* output1 = NULL;
    hr = output->QueryInterface(__uuidof(IDXGIOutput1), (void**)&output1);
    SAFE_RELEASE(output);
    if (FAILED(hr)) return hr;

    hr = output1->DuplicateOutput(g_device, &g_duplication);
    SAFE_RELEASE(output1);
    return hr;
}

/* ---- capture_init --------------------------------------------- */

EXPORT int capture_init(int monitor_index, int out_width, int out_height)
{
    if (g_initialized) return 0;

    g_monitorIdx = monitor_index;
    g_outW       = out_width;
    g_outH       = out_height;

    /* D3D11 device */
    D3D_FEATURE_LEVEL fl;
    D3D_FEATURE_LEVEL levels[] = { D3D_FEATURE_LEVEL_11_0 };
    HRESULT hr = D3D11CreateDevice(
        NULL,                       /* default adapter */
        D3D_DRIVER_TYPE_HARDWARE,
        NULL,                       /* no software rasterizer */
        0,                          /* flags */
        levels,                     /* feature levels array */
        1,                          /* number of feature levels */
        D3D11_SDK_VERSION,
        &g_device,
        &fl,
        &g_context
    );
    if (FAILED(hr)) return -1;

    /* Desktop Duplication */
    hr = acquire_duplication();
    if (FAILED(hr)) return -6;

    /* Staging texture */
    hr = ensure_staging();
    if (FAILED(hr)) return -8;

    g_initialized = TRUE;
    return 0;
}

/* ---- capture_grab --------------------------------------------- */

EXPORT int capture_grab(unsigned char* out_buf, int buf_size)
{
    if (!g_initialized) return -1;

    int needed = g_outW * g_outH * 4;
    if (buf_size < needed) return -1;

    DXGI_OUTDUPL_FRAME_INFO fi;
    IDXGIResource* res = NULL;

    HRESULT hr = g_duplication->AcquireNextFrame(0, &fi, &res);
    if (hr == DXGI_ERROR_WAIT_TIMEOUT)
        return 0;   /* no new frame */
    if (FAILED(hr))
        return -2;  /* access lost etc. */

    ID3D11Texture2D* tex = NULL;
    hr = res->QueryInterface(__uuidof(ID3D11Texture2D), (void**)&tex);
    SAFE_RELEASE(res);
    if (FAILED(hr)) {
        g_duplication->ReleaseFrame();
        return -2;
    }

    /* full texture -> staging (DMA, low CPU cost) */
    ensure_staging();
    g_context->CopyResource(g_staging, tex);

    SAFE_RELEASE(tex);
    g_duplication->ReleaseFrame();

    /* Map staging -> subsample into out_buf */
    D3D11_MAPPED_SUBRESOURCE mapped;
    hr = g_context->Map(g_staging, 0, D3D11_MAP_READ, 0, &mapped);
    if (FAILED(hr)) return -3;

    unsigned char* src = (unsigned char*)mapped.pData;
    int pitch = (int)mapped.RowPitch;

    int outW = g_outW;
    int outH = g_outH;
    int scrW = g_screenW;
    int scrH = g_screenH;

    for (int r = 0; r < outH; r++) {
        int sy = (int)(((double)r + 0.5) / outH * scrH);
        if (sy >= scrH) sy = scrH - 1;

        unsigned char* srow = src + (size_t)sy * pitch;
        unsigned char* drow = out_buf + r * outW * 4;

        for (int c = 0; c < outW; c++) {
            int sx = (int)(((double)c + 0.5) / outW * scrW);
            if (sx >= scrW) sx = scrW - 1;

            unsigned char* sp = srow + sx * 4;
            unsigned char* dp = drow + c * 4;
            dp[0] = sp[0];
            dp[1] = sp[1];
            dp[2] = sp[2];
            dp[3] = sp[3];
        }
    }

    g_context->Unmap(g_staging, 0);
    return 1;  /* new frame */
}

/* ---- helpers -------------------------------------------------- */

EXPORT int  capture_get_width(void)  { return g_screenW; }
EXPORT int  capture_get_height(void) { return g_screenH; }

EXPORT void capture_reset(void)
{
    SAFE_RELEASE(g_staging);
    g_stagingW = 0;
    g_stagingH = 0;
    acquire_duplication();
}

EXPORT void capture_cleanup(void)
{
    SAFE_RELEASE(g_staging);
    SAFE_RELEASE(g_duplication);
    SAFE_RELEASE(g_context);
    SAFE_RELEASE(g_device);
    g_stagingW = 0;
    g_stagingH = 0;
    g_initialized = FALSE;
}

/* ---- DllMain -------------------------------------------------- */

BOOL WINAPI DllMain(HINSTANCE hDLL, DWORD reason, LPVOID reserved)
{
    (void)hDLL; (void)reserved;
    if (reason == DLL_PROCESS_DETACH)
        capture_cleanup();
    return TRUE;
}
