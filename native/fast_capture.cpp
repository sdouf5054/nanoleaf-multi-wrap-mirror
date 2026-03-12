/*
 * fast_capture.cpp - DXGI Desktop Duplication + CPU subsample DLL
 *
 * [Change] Support for portrait mode (rotated displays)
 * - g_rotation: stores DXGI_OUTPUT_DESC.Rotation
 * - g_texW, g_texH: actual size of the staging texture (physical panel orientation)
 * - capture_grab: converts subsampling coordinates to texture coordinates based on rotation
 * - capture_get_rotation: allows external query of current rotation state
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
static int  g_screenW     = 0;     /* logical desktop size (DesktopCoordinates) */
static int  g_screenH     = 0;
static int  g_texW        = 0;     /* actual texture size (physical panel orientation) */
static int  g_texH        = 0;
static int  g_stagingW    = 0;
static int  g_stagingH    = 0;
static int  g_monitorIdx  = 0;
static int  g_rotation    = 0;     /* DXGI_MODE_ROTATION value (0=Identity) */
static BOOL g_initialized = FALSE;

#define SAFE_RELEASE(p) do { if (p) { (p)->Release(); (p) = NULL; } } while(0)
#define EXPORT extern "C" __declspec(dllexport)

/* ---- internal: create staging texture ------------------------- */

static HRESULT ensure_staging(void)
{
    /* Create staging based on actual texture size (g_texW x g_texH) */
    if (g_staging && g_stagingW == g_texW && g_stagingH == g_texH)
        return S_OK;

    SAFE_RELEASE(g_staging);

    D3D11_TEXTURE2D_DESC desc;
    memset(&desc, 0, sizeof(desc));
    desc.Width              = (UINT)g_texW;
    desc.Height             = (UINT)g_texH;
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
        g_stagingW = g_texW;
        g_stagingH = g_texH;
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

    /* Logical desktop size (rotation-adjusted coordinates) */
    g_screenW = odesc.DesktopCoordinates.right  - odesc.DesktopCoordinates.left;
    g_screenH = odesc.DesktopCoordinates.bottom - odesc.DesktopCoordinates.top;

    /* Store rotation info */
    g_rotation = (int)odesc.Rotation;
    /* DXGI_MODE_ROTATION:
     *   0 = DXGI_MODE_ROTATION_UNSPECIFIED
     *   1 = DXGI_MODE_ROTATION_IDENTITY    (no rotation)
     *   2 = DXGI_MODE_ROTATION_ROTATE90    (90 degrees clockwise)
     *   3 = DXGI_MODE_ROTATION_ROTATE180
     *   4 = DXGI_MODE_ROTATION_ROTATE270   (90 degrees counter-clockwise)
     */

    /* Determine actual texture size.
     * When rotated, the texture is in physical panel orientation, so W/H are swapped.
     * Example: portrait mode (ROTATE90) - logical 1440x2560, texture 2560x1440
     */
    if (g_rotation == 2 || g_rotation == 4) {
        /* 90 or 270 degree rotation: texture W/H are logical H/W */
        g_texW = g_screenH;
        g_texH = g_screenW;
    } else {
        /* No rotation or 180 degrees: texture matches logical size */
        g_texW = g_screenW;
        g_texH = g_screenH;
    }

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
        NULL,
        D3D_DRIVER_TYPE_HARDWARE,
        NULL,
        0,
        levels,
        1,
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
    int scrW = g_screenW;   /* logical width (e.g. 1440 in portrait mode) */
    int scrH = g_screenH;   /* logical height (e.g. 2560 in portrait mode) */
    int texW = g_texW;      /* actual texture width */
    int texH = g_texH;
    int rot  = g_rotation;

    for (int r = 0; r < outH; r++) {
        int ly = (int)(((double)r + 0.5) / outH * scrH);
        if (ly >= scrH) ly = scrH - 1;

        unsigned char* drow = out_buf + r * outW * 4;

        for (int c = 0; c < outW; c++) {
            int lx = (int)(((double)c + 0.5) / outW * scrW);
            if (lx >= scrW) lx = scrW - 1;

            int tx, ty;

            switch (rot) {
            case 2:  /* DXGI_MODE_ROTATION_ROTATE90 */
                tx = ly;
                ty = texH - 1 - lx;
                break;

            case 4:  /* DXGI_MODE_ROTATION_ROTATE270 */
                tx = texW - 1 - ly;
                ty = lx;
                break;

            case 3:  /* DXGI_MODE_ROTATION_ROTATE180 */
                tx = texW - 1 - lx;
                ty = texH - 1 - ly;
                break;

            default: /* IDENTITY / UNSPECIFIED */
                tx = lx;
                ty = ly;
                break;
            }

            /* Clamp to valid range */
            if (tx < 0) tx = 0;
            if (tx >= texW) tx = texW - 1;
            if (ty < 0) ty = 0;
            if (ty >= texH) ty = texH - 1;

            unsigned char* sp = src + (size_t)ty * pitch + tx * 4;
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

EXPORT int  capture_get_width(void)    { return g_screenW; }
EXPORT int  capture_get_height(void)   { return g_screenH; }
EXPORT int  capture_get_rotation(void) { return g_rotation; }  /* added */

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
    g_texW     = 0;
    g_texH     = 0;
    g_rotation = 0;
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
