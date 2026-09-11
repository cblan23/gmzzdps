#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "zstd.h"
#include "decompress/zstd_decompress_internal.h"

#if defined(_WIN32)
#  define ZSH_EXPORT __declspec(dllexport)
#else
#  define ZSH_EXPORT
#endif

typedef struct {
    ZSTD_DCtx* dctx;
    unsigned char* ring;
    unsigned char* history;
    size_t ring_capacity;
    size_t ring_position;
    size_t block_capacity;
    size_t history_size;
    size_t window_size;
} ZSH_State;

static void zsh_error(char* error, size_t capacity, const char* message)
{
    if (error == NULL || capacity == 0) return;
    if (message == NULL) message = "unknown error";
#if defined(_MSC_VER)
    _snprintf_s(error, capacity, _TRUNCATE, "%s", message);
#else
    snprintf(error, capacity, "%s", message);
#endif
}

static int zsh_copy_table(
    void* destination,
    size_t destination_capacity,
    const void* source,
    size_t source_size,
    char* error,
    size_t error_capacity)
{
    if (source == NULL || source_size == 0) {
        zsh_error(error, error_capacity, "missing entropy table");
        return 0;
    }
    if (source_size > destination_capacity) {
        zsh_error(error, error_capacity, "entropy table is too large");
        return 0;
    }
    memcpy(destination, source, source_size);
    return 1;
}

ZSH_EXPORT uint32_t zsh_version_number(void)
{
    return ZSTD_VERSION_NUMBER;
}

ZSH_EXPORT size_t zsh_context_size(void)
{
    return sizeof(ZSTD_DCtx);
}

ZSH_EXPORT size_t zsh_context_entropy_offset(void)
{
    return offsetof(ZSTD_DCtx, entropy);
}

ZSH_EXPORT size_t zsh_context_previous_dst_end_offset(void)
{
    return offsetof(ZSTD_DCtx, previousDstEnd);
}

ZSH_EXPORT size_t zsh_context_prefix_start_offset(void)
{
    return offsetof(ZSTD_DCtx, prefixStart);
}

ZSH_EXPORT size_t zsh_context_virtual_start_offset(void)
{
    return offsetof(ZSTD_DCtx, virtualStart);
}

ZSH_EXPORT size_t zsh_context_dict_end_offset(void)
{
    return offsetof(ZSTD_DCtx, dictEnd);
}

ZSH_EXPORT size_t zsh_context_frame_params_offset(void)
{
    return offsetof(ZSTD_DCtx, fParams);
}

ZSH_EXPORT size_t zsh_ll_table_capacity(void)
{
    ZSTD_DCtx* dctx = (ZSTD_DCtx*)0;
    return sizeof(dctx->entropy.LLTable);
}

ZSH_EXPORT size_t zsh_ml_table_capacity(void)
{
    ZSTD_DCtx* dctx = (ZSTD_DCtx*)0;
    return sizeof(dctx->entropy.MLTable);
}

ZSH_EXPORT size_t zsh_of_table_capacity(void)
{
    ZSTD_DCtx* dctx = (ZSTD_DCtx*)0;
    return sizeof(dctx->entropy.OFTable);
}

ZSH_EXPORT size_t zsh_huf_table_capacity(void)
{
    ZSTD_DCtx* dctx = (ZSTD_DCtx*)0;
    return sizeof(dctx->entropy.hufTable);
}

ZSH_EXPORT void* zsh_create(
    const void* remote_context,
    size_t remote_context_size,
    const void* history,
    size_t history_size,
    const void* ll_table,
    size_t ll_table_size,
    const void* ml_table,
    size_t ml_table_size,
    const void* of_table,
    size_t of_table_size,
    const void* huf_table,
    size_t huf_table_size,
    char* error,
    size_t error_capacity)
{
    const ZSTD_DCtx* remote;
    ZSH_State* state;
    ZSTD_DCtx* local;
    size_t window_size;
    size_t block_capacity;
    size_t ring_capacity;

    if (remote_context == NULL || remote_context_size < sizeof(ZSTD_DCtx)) {
        zsh_error(error, error_capacity, "remote context is truncated");
        return NULL;
    }
    remote = (const ZSTD_DCtx*)remote_context;
    window_size = (size_t)remote->fParams.windowSize;
    block_capacity = (size_t)remote->fParams.blockSizeMax;
    if (window_size == 0 || window_size > (128U << 20)) {
        zsh_error(error, error_capacity, "invalid window size");
        return NULL;
    }
    if (block_capacity == 0 || block_capacity > ZSTD_BLOCKSIZE_MAX) {
        zsh_error(error, error_capacity, "invalid block size");
        return NULL;
    }
    if (history == NULL || history_size == 0 || history_size > window_size) {
        zsh_error(error, error_capacity, "invalid history window");
        return NULL;
    }

    state = (ZSH_State*)calloc(1, sizeof(*state));
    if (state == NULL) {
        zsh_error(error, error_capacity, "state allocation failed");
        return NULL;
    }
    local = ZSTD_createDCtx();
    if (local == NULL) {
        free(state);
        zsh_error(error, error_capacity, "ZSTD_createDCtx failed");
        return NULL;
    }
    ring_capacity = history_size + (block_capacity * 2) + 128;
    state->ring = (unsigned char*)malloc(ring_capacity);
    state->history = (unsigned char*)malloc(window_size);
    if (state->ring == NULL || state->history == NULL) {
        free(state->history);
        free(state->ring);
        ZSTD_freeDCtx(local);
        free(state);
        zsh_error(error, error_capacity, "history allocation failed");
        return NULL;
    }
    memcpy(state->ring, history, history_size);
    memcpy(state->history, history, history_size);

    memcpy(&local->entropy, &remote->entropy, sizeof(local->entropy));
    memcpy(&local->workspace, &remote->workspace, sizeof(local->workspace));
    if (!zsh_copy_table(
            local->entropy.LLTable, sizeof(local->entropy.LLTable),
            ll_table, ll_table_size, error, error_capacity)
        || !zsh_copy_table(
            local->entropy.MLTable, sizeof(local->entropy.MLTable),
            ml_table, ml_table_size, error, error_capacity)
        || !zsh_copy_table(
            local->entropy.OFTable, sizeof(local->entropy.OFTable),
            of_table, of_table_size, error, error_capacity)
        || !zsh_copy_table(
            local->entropy.hufTable, sizeof(local->entropy.hufTable),
            huf_table, huf_table_size, error, error_capacity)) {
        free(state->history);
        free(state->ring);
        ZSTD_freeDCtx(local);
        free(state);
        return NULL;
    }
    local->LLTptr = local->entropy.LLTable;
    local->MLTptr = local->entropy.MLTable;
    local->OFTptr = local->entropy.OFTable;
    local->HUFptr = local->entropy.hufTable;

    local->expected = remote->expected;
    local->fParams = remote->fParams;
    local->processedCSize = remote->processedCSize;
    local->decodedSize = remote->decodedSize;
    local->bType = remote->bType;
    local->stage = remote->stage;
    local->litEntropy = remote->litEntropy;
    local->fseEntropy = remote->fseEntropy;
    local->format = remote->format;
    local->forceIgnoreChecksum = remote->forceIgnoreChecksum;
    local->validateChecksum = 0;
    local->dictID = remote->dictID;
    local->ddictIsCold = 0;
    local->dictUses = ZSTD_dont_use;
    local->ddict = NULL;
    local->ddictLocal = NULL;
    local->ddictSet = NULL;
    local->disableHufAsm = remote->disableHufAsm;
    local->maxBlockSizeParam = remote->maxBlockSizeParam;
    local->isFrameDecompression = 0;
    local->litBuffer = local->litExtraBuffer;
    local->litBufferEnd = local->litExtraBuffer + sizeof(local->litExtraBuffer);
    local->litBufferLocation = ZSTD_not_in_dst;

    local->prefixStart = state->ring;
    local->virtualStart = state->ring;
    local->dictEnd = state->ring;
    local->previousDstEnd = state->ring + history_size;

    state->dctx = local;
    state->ring_capacity = ring_capacity;
    state->ring_position = history_size;
    state->block_capacity = block_capacity;
    state->history_size = history_size;
    state->window_size = window_size;
    if (error != NULL && error_capacity != 0) error[0] = '\0';
    return state;
}

ZSH_EXPORT void* zsh_create_fresh(
    size_t window_size,
    size_t block_capacity,
    char* error,
    size_t error_capacity)
{
    ZSH_State* state;
    size_t ring_capacity;
    if (window_size == 0 || window_size > (128U << 20)
        || block_capacity == 0 || block_capacity > ZSTD_BLOCKSIZE_MAX) {
        zsh_error(error, error_capacity, "invalid fresh decoder sizes");
        return NULL;
    }
    state = (ZSH_State*)calloc(1, sizeof(*state));
    if (state == NULL) {
        zsh_error(error, error_capacity, "state allocation failed");
        return NULL;
    }
    state->dctx = ZSTD_createDCtx();
    ring_capacity = window_size + (block_capacity * 2) + 128;
    state->ring = (unsigned char*)malloc(ring_capacity);
    state->history = (unsigned char*)malloc(window_size);
    if (state->dctx == NULL || state->ring == NULL || state->history == NULL) {
        if (state->dctx != NULL) ZSTD_freeDCtx(state->dctx);
        free(state->history);
        free(state->ring);
        free(state);
        zsh_error(error, error_capacity, "fresh decoder allocation failed");
        return NULL;
    }
    if (ZSTD_isError(ZSTD_decompressBegin(state->dctx))) {
        ZSTD_freeDCtx(state->dctx);
        free(state->history);
        free(state->ring);
        free(state);
        zsh_error(error, error_capacity, "ZSTD_decompressBegin failed");
        return NULL;
    }
    state->dctx->fParams.windowSize = window_size;
    state->dctx->fParams.blockSizeMax = (unsigned)block_capacity;
    state->dctx->prefixStart = state->ring;
    state->dctx->virtualStart = state->ring;
    state->dctx->dictEnd = state->ring;
    state->dctx->previousDstEnd = state->ring;
    state->ring_capacity = ring_capacity;
    state->block_capacity = block_capacity;
    state->window_size = window_size;
    if (error != NULL && error_capacity != 0) error[0] = '\0';
    return state;
}

static size_t zsh_seq_table_size(const ZSTD_seqSymbol* table, size_t capacity)
{
    const ZSTD_seqSymbol_header* header = (const ZSTD_seqSymbol_header*)table;
    size_t size;
    if (table == NULL || header->tableLog > 12) return 0;
    size = (1 + ((size_t)1 << header->tableLog)) * sizeof(ZSTD_seqSymbol);
    return size <= capacity ? size : 0;
}

ZSH_EXPORT void* zsh_clone(void* opaque, char* error, size_t error_capacity)
{
    ZSH_State* state = (ZSH_State*)opaque;
    if (state == NULL || state->dctx == NULL || state->history_size == 0) {
        zsh_error(error, error_capacity, "decoder has no restorable history");
        return NULL;
    }
    return zsh_create(
        state->dctx,
        sizeof(*state->dctx),
        state->history,
        state->history_size,
        state->dctx->LLTptr,
        zsh_seq_table_size(
            state->dctx->LLTptr, sizeof(state->dctx->entropy.LLTable)),
        state->dctx->MLTptr,
        zsh_seq_table_size(
            state->dctx->MLTptr, sizeof(state->dctx->entropy.MLTable)),
        state->dctx->OFTptr,
        zsh_seq_table_size(
            state->dctx->OFTptr, sizeof(state->dctx->entropy.OFTable)),
        state->dctx->HUFptr,
        sizeof(state->dctx->entropy.hufTable),
        error,
        error_capacity);
}

ZSH_EXPORT int zsh_decode_block(
    void* opaque,
    const void* block,
    size_t block_size,
    void* output,
    size_t output_capacity,
    size_t* output_size,
    char* error,
    size_t error_capacity)
{
    ZSH_State* state = (ZSH_State*)opaque;
    const unsigned char* source = (const unsigned char*)block;
    unsigned char* destination;
    uint32_t header;
    unsigned block_type;
    size_t encoded_size;
    size_t payload_size;
    size_t decoded_size;

    if (output_size != NULL) *output_size = 0;
    if (state == NULL || state->dctx == NULL || block == NULL || block_size < 3) {
        zsh_error(error, error_capacity, "invalid decoder input");
        return 0;
    }
    header = (uint32_t)source[0]
           | ((uint32_t)source[1] << 8)
           | ((uint32_t)source[2] << 16);
    block_type = (header >> 1) & 3U;
    encoded_size = (size_t)(header >> 3);
    payload_size = block_type == 1 ? 1 : encoded_size;
    if (block_type == 3 || block_size != payload_size + 3) {
        zsh_error(error, error_capacity, "invalid Zstd block header");
        return 0;
    }
    if (encoded_size > state->block_capacity || encoded_size > output_capacity) {
        zsh_error(error, error_capacity, "decoded block exceeds capacity");
        return 0;
    }
    if (state->ring_position + state->block_capacity + 64 > state->ring_capacity) {
        state->ring_position = 0;
    }
    destination = state->ring + state->ring_position;

    if (block_type == 0) {
        memcpy(destination, source + 3, encoded_size);
        decoded_size = encoded_size;
        ZSTD_insertBlock(state->dctx, destination, decoded_size);
    } else if (block_type == 1) {
        memset(destination, source[3], encoded_size);
        decoded_size = encoded_size;
        ZSTD_insertBlock(state->dctx, destination, decoded_size);
    } else {
        decoded_size = ZSTD_decompressBlock(
            state->dctx,
            destination,
            state->block_capacity,
            source + 3,
            encoded_size);
        if (ZSTD_isError(decoded_size)) {
            zsh_error(error, error_capacity, ZSTD_getErrorName(decoded_size));
            return 0;
        }
    }
    if (decoded_size > output_capacity) {
        zsh_error(error, error_capacity, "output buffer is too small");
        return 0;
    }
    if (decoded_size != 0 && output != NULL) {
        memcpy(output, destination, decoded_size);
    }
    state->ring_position += decoded_size;
    if (decoded_size >= state->window_size) {
        memcpy(
            state->history,
            destination + decoded_size - state->window_size,
            state->window_size);
        state->history_size = state->window_size;
    } else if (decoded_size != 0) {
        size_t overflow = state->history_size + decoded_size > state->window_size
            ? state->history_size + decoded_size - state->window_size
            : 0;
        if (overflow != 0) {
            memmove(
                state->history,
                state->history + overflow,
                state->history_size - overflow);
            state->history_size -= overflow;
        }
        memcpy(state->history + state->history_size, destination, decoded_size);
        state->history_size += decoded_size;
    }
    if (output_size != NULL) *output_size = decoded_size;
    if (error != NULL && error_capacity != 0) error[0] = '\0';
    return 1;
}

ZSH_EXPORT void zsh_free(void* opaque)
{
    ZSH_State* state = (ZSH_State*)opaque;
    if (state == NULL) return;
    if (state->dctx != NULL) ZSTD_freeDCtx(state->dctx);
    free(state->history);
    free(state->ring);
    free(state);
}
