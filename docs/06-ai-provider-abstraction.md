# 06 - AI Provider Abstraction Layer

## Purpose

The AI provider abstraction layer allows ShareSentinel to use any of three AI providers (Anthropic Claude, OpenAI GPT, Google Gemini) interchangeably. Switching providers requires only a configuration change, not a code change. The layer also manages prompt templates, enforces structured output, tracks costs, and provides consistent error handling across providers.

## Provider Interface

All providers implement a common abstract interface:

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, List

@dataclass
class AnalysisRequest:
    """Input to the AI provider."""
    mode: str                              # "text", "multimodal", "filename_only"
    text_content: Optional[str] = None     # Extracted text (for text mode)
    images: Optional[List[bytes]] = None   # Image bytes (for multimodal mode)
    image_mime_types: Optional[List[str]] = None  # MIME types for each image
    file_name: str = ""
    file_path: str = ""
    file_size: int = 0
    sharing_user: str = ""
    sharing_type: str = ""
    sharing_permission: str = ""
    event_time: str = ""
    was_sampled: bool = False
    sampling_description: str = ""
    file_metadata: dict = field(default_factory=dict)
    filename_flagged: bool = False
    filename_flag_keywords: List[str] = field(default_factory=list)

@dataclass
class AnalysisResponse:
    """Output from the AI provider."""
    sensitivity_rating: int          # 1-5
    categories_detected: List[str]   # e.g., ["PII - Tax Documents", "Financial Information"]
    summary: str                     # Brief description of findings
    confidence: str                  # "high", "medium", "low"
    recommendation: str              # What action to take
    raw_response: str                # The full raw response from the AI (for debugging)
    provider: str                    # "anthropic", "openai", "gemini"
    model: str                       # Specific model used (e.g., "claude-sonnet-4-5-20250929")
    input_tokens: int                # Tokens consumed for input
    output_tokens: int               # Tokens consumed for output
    estimated_cost_usd: float        # Estimated cost in USD
    processing_time_seconds: float   # Time taken for the API call
    success: bool = True
    error: Optional[str] = None

class BaseAIProvider(ABC):
    """Abstract base class for AI providers."""
    
    @abstractmethod
    async def analyze(self, request: AnalysisRequest) -> AnalysisResponse:
        """Send content to the AI for sensitivity analysis."""
        pass
    
    @abstractmethod
    def get_provider_name(self) -> str:
        pass
    
    @abstractmethod
    def get_model_name(self) -> str:
        pass
```

## Prompt Management

The AI prompt is stored as a configurable template in `config/prompt_templates/sensitivity_analysis.txt`. This allows updating the sensitivity criteria without redeploying the container.

### System Prompt (used for all analysis modes):

```
You are an AI assistant tasked with scanning files for sensitive information. You work for an organization's information security team. Your goal is to identify documents that, if exposed to a wide audience via anonymous or organization-wide sharing links, could cause concern, reputational damage, policy violations, or harm to the individuals mentioned in the document.

You must evaluate the content and return a structured JSON response. Do not include any text outside the JSON object.
```

### User Prompt Template (text-based analysis):

```
Analyze the following file content for sensitive information.

FILE METADATA:
- File name: {file_name}
- File path: {file_path}
- File size: {file_size_human}
- Shared by: {sharing_user}
- Sharing type: {sharing_type} with {sharing_permission} access
- Sharing event time: {event_time}
{filename_flag_notice}
{sampling_notice}
{metadata_section}

FILE CONTENT:
{text_content}

EVALUATION CRITERIA:
Flag files containing any of the following:

1. Personally Identifiable Information (PII):
   - Driver's licenses, government-issued IDs
   - Tax returns, W-2 forms, 1099 forms
   - Birth certificates
   - Passport numbers
   - Health insurance cards, medical record numbers
   - Financial account numbers, routing numbers
   - Social Security Numbers
   - Biometric data
   - Credit card numbers

2. Student/Academic Records (FERPA-protected):
   - Academic transcripts, grade reports
   - Disciplinary records
   - Class schedules with student names
   - Student contact details, birth dates

3. Protected Health Information (HIPAA-related):
   - Medical records, diagnoses
   - Therapy or counseling notes
   - Prescription information
   - Health insurance details

4. Sensitive Organizational Information:
   - Salary/compensation data for identifiable individuals
   - Performance reviews, disciplinary notes
   - Sensitive meeting notes (HR, legal, executive)
   - Legal documents, litigation materials
   - Financial contracts with non-public terms
   - Proprietary business information marked as confidential
   - Security credentials, passwords, API keys

5. Personal Information That Could Be Embarrassing or Harmful:
   - Private personal correspondence
   - Photos or documents of a personal nature
   - Content revealing protected characteristics
   - Anything that could cause reputational harm if widely shared

RESPONSE FORMAT:
Return ONLY a JSON object with the following structure. No markdown, no code fences, no extra text:
{{
    "sensitivity_rating": <integer 1-5>,
    "categories_detected": [<list of category strings detected>],
    "summary": "<brief description of sensitive content found, or explanation of why the file is not sensitive>",
    "confidence": "<high|medium|low>",
    "recommendation": "<recommended action>"
}}

RATING SCALE:
1 = No sensitive information detected. File appears safe for broad sharing.
2 = Minor sensitivity. Contains some personal information but unlikely to cause harm (e.g., a shared contact list with only names and work emails).
3 = Moderate sensitivity. Contains information that is somewhat sensitive but may be acceptable for org-wide sharing depending on context (e.g., a project budget without salary details).
4 = High sensitivity. Contains information that should NOT be shared broadly. Analyst should review and likely contact the user (e.g., a spreadsheet with employee home addresses, a document with student grades).
5 = Critical sensitivity. Contains highly sensitive PII or information that could cause significant harm (e.g., tax returns, medical records, SSN lists, passwords). Requires immediate analyst attention.
```

### User Prompt Template (multimodal analysis):

Same as the text-based template but replace the `FILE CONTENT` section with:

```
FILE CONTENT:
[The file content is provided as {image_count} image(s) attached to this message. {page_context}]

Please analyze the attached images carefully. Look for any visible text, form fields, data tables, handwritten notes, or other content that may contain sensitive information.
```

Where `{page_context}` is, for example: "These are pages 1-3 of a 12-page PDF document."

### User Prompt Template (filename/path-only analysis):

```
Analyze the following file based ONLY on its filename, path, and metadata. The file content was not available for direct analysis because: {reason}.

FILE METADATA:
- File name: {file_name}
- File path: {file_path}
- File size: {file_size_human}
- File type: {file_extension}
- Shared by: {sharing_user}
- Sharing type: {sharing_type} with {sharing_permission} access
- Sharing event time: {event_time}
{filename_flag_notice}
{archive_manifest}

Since you can only see the filename and metadata (not the file content), base your assessment on:
- Does the filename suggest sensitive content? (e.g., "tax_return_2024.pdf", "employee_SSN_list.xlsx")
- Does the file path suggest a sensitive location? (e.g., "/HR/Confidential/", "/Legal/Litigation/")
- Does the file extension combined with the name suggest risk?

If you cannot determine sensitivity from the filename alone, assign a rating of 3 (moderate/unknown) with a note that the file content was not inspected and should be manually reviewed if the filename raises any concern.

RESPONSE FORMAT:
Return ONLY a JSON object with the following structure. No markdown, no code fences, no extra text:
{{
    "sensitivity_rating": <integer 1-5>,
    "categories_detected": [<list of category strings or "filename_analysis_only">],
    "summary": "<assessment based on filename and metadata>",
    "confidence": "<high|medium|low - likely 'low' for filename-only analysis>",
    "recommendation": "<recommended action>"
}}
```

### Template Variables

The prompt manager fills in these variables at runtime:

| Variable | Source |
|----------|--------|
| `{file_name}` | From job payload / Graph API metadata |
| `{file_path}` | From job payload (SourceRelativeUrl) |
| `{file_size_human}` | Computed from bytes (e.g., "2.3 MB") |
| `{sharing_user}` | From job payload (UserId) |
| `{sharing_type}` | "Anonymous" or "Organization-wide" |
| `{sharing_permission}` | "View" or "Edit" |
| `{event_time}` | From job payload (CreationTime) |
| `{filename_flag_notice}` | "NOTICE: Filename matches sensitivity keywords: [tax, ssn]" or empty |
| `{sampling_notice}` | "NOTE: This is a sample of the file. {sampling_description}" or empty |
| `{metadata_section}` | Formatted document properties (title, author, sheet names, etc.) |
| `{text_content}` | The extracted text content |
| `{image_count}` | Number of images attached |
| `{page_context}` | "Pages 1-3 of a 12-page PDF" |
| `{reason}` | Why content wasn't available (e.g., "File type excluded", "File too large") |
| `{archive_manifest}` | ZIP file listing (for archive files) |

## Provider Implementations

### Anthropic Claude Provider

```python
import anthropic
import time
import json

class AnthropicProvider(BaseAIProvider):
    # Pricing per 1M tokens (update as pricing changes)
    PRICING = {
        "claude-sonnet-4-5-20250929": {"input": 3.00, "output": 15.00},
        "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    }
    
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-5-20250929"):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
    
    async def analyze(self, request: AnalysisRequest) -> AnalysisResponse:
        start_time = time.time()
        
        try:
            messages = self._build_messages(request)
            
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                temperature=0,  # Deterministic output
                system=SYSTEM_PROMPT,
                messages=messages,
            )
            
            processing_time = time.time() - start_time
            
            # Parse the structured response
            raw_text = response.content[0].text
            parsed = self._parse_response(raw_text)
            
            # Calculate cost
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            pricing = self.PRICING.get(self.model, {"input": 0, "output": 0})
            cost = (input_tokens * pricing["input"] / 1_000_000) + \
                   (output_tokens * pricing["output"] / 1_000_000)
            
            return AnalysisResponse(
                sensitivity_rating=parsed["sensitivity_rating"],
                categories_detected=parsed["categories_detected"],
                summary=parsed["summary"],
                confidence=parsed["confidence"],
                recommendation=parsed["recommendation"],
                raw_response=raw_text,
                provider="anthropic",
                model=self.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=cost,
                processing_time_seconds=processing_time,
            )
        except Exception as e:
            return AnalysisResponse(
                sensitivity_rating=0,
                categories_detected=[],
                summary="",
                confidence="",
                recommendation="",
                raw_response="",
                provider="anthropic",
                model=self.model,
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd=0,
                processing_time_seconds=time.time() - start_time,
                success=False,
                error=str(e),
            )
    
    def _build_messages(self, request: AnalysisRequest) -> list:
        """Build the messages array for the Anthropic API."""
        if request.mode == "multimodal" and request.images:
            # Multimodal: include images in the message
            content = []
            for img_bytes, mime_type in zip(request.images, request.image_mime_types):
                import base64
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime_type,
                        "data": base64.b64encode(img_bytes).decode('utf-8'),
                    }
                })
            content.append({
                "type": "text",
                "text": self._render_prompt(request),
            })
            return [{"role": "user", "content": content}]
        else:
            # Text-only or filename-only
            return [{"role": "user", "content": self._render_prompt(request)}]
```

### OpenAI GPT Provider

```python
import openai

class OpenAIProvider(BaseAIProvider):
    PRICING = {
        "gpt-4o": {"input": 2.50, "output": 10.00},
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    }
    
    def __init__(self, api_key: str, model: str = "gpt-4o"):
        self.client = openai.OpenAI(api_key=api_key)
        self.model = model
    
    # Similar implementation to Anthropic but using OpenAI API format
    # For multimodal: use the image_url content type with base64 data URLs
    # Use response_format={"type": "json_object"} for structured output
```

### Google Gemini Provider

```python
import google.generativeai as genai

class GeminiProvider(BaseAIProvider):
    PRICING = {
        "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
        "gemini-2.5-pro-preview-05-06": {"input": 1.25, "output": 10.00},
    }
    
    def __init__(self, api_key: str, model: str = "gemini-2.0-flash"):
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(model)
        self.model_name = model
    
    # Similar implementation using Google's generativeai SDK
    # For multimodal: pass PIL Image objects or base64 data
    # Use response_mime_type="application/json" for structured output
```

## Response Parsing

The response parser handles the structured JSON output from all providers, with fallback handling for malformed responses.

```python
import json
import re

def parse_ai_response(raw_text: str) -> dict:
    """
    Parse the AI response into a structured dict.
    Handles common formatting issues (markdown code fences, extra text, etc.)
    """
    # Strip markdown code fences if present
    cleaned = raw_text.strip()
    cleaned = re.sub(r'^```json\s*', '', cleaned)
    cleaned = re.sub(r'^```\s*', '', cleaned)
    cleaned = re.sub(r'\s*```$', '', cleaned)
    cleaned = cleaned.strip()
    
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        match = re.search(r'\{[^{}]*\}', cleaned, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                # Complete failure to parse
                return {
                    "sensitivity_rating": 3,  # Default to moderate when parsing fails
                    "categories_detected": ["parse_error"],
                    "summary": f"Failed to parse AI response. Raw response: {raw_text[:500]}",
                    "confidence": "low",
                    "recommendation": "Manual review recommended due to AI response parsing failure.",
                }
        else:
            return {
                "sensitivity_rating": 3,
                "categories_detected": ["parse_error"],
                "summary": f"No JSON found in AI response. Raw response: {raw_text[:500]}",
                "confidence": "low",
                "recommendation": "Manual review recommended due to AI response parsing failure.",
            }
    
    # Validate and sanitize fields
    result = {
        "sensitivity_rating": _clamp(parsed.get("sensitivity_rating", 3), 1, 5),
        "categories_detected": parsed.get("categories_detected", []),
        "summary": str(parsed.get("summary", ""))[:2000],  # Limit summary length
        "confidence": parsed.get("confidence", "medium"),
        "recommendation": str(parsed.get("recommendation", ""))[:1000],
    }
    
    # Ensure categories is a list
    if not isinstance(result["categories_detected"], list):
        result["categories_detected"] = [str(result["categories_detected"])]
    
    # Ensure confidence is valid
    if result["confidence"] not in ("high", "medium", "low"):
        result["confidence"] = "medium"
    
    return result

def _clamp(value, min_val, max_val):
    try:
        return max(min_val, min(max_val, int(value)))
    except (ValueError, TypeError):
        return 3  # Default to moderate
```

## Provider Factory

```python
def create_provider(config: dict) -> BaseAIProvider:
    """Create an AI provider instance based on configuration."""
    provider_name = config["ai_provider"]  # "anthropic", "openai", "gemini"
    
    if provider_name == "anthropic":
        return AnthropicProvider(
            api_key=config["anthropic_api_key"],
            model=config.get("anthropic_model", "claude-sonnet-4-5-20250929"),
        )
    elif provider_name == "openai":
        return OpenAIProvider(
            api_key=config["openai_api_key"],
            model=config.get("openai_model", "gpt-4o"),
        )
    elif provider_name == "gemini":
        return GeminiProvider(
            api_key=config["gemini_api_key"],
            model=config.get("gemini_model", "gemini-2.0-flash"),
        )
    else:
        raise ValueError(f"Unknown AI provider: {provider_name}")
```

## Cost Tracking

Every AI analysis call logs:
- Provider and model used
- Input token count
- Output token count
- Estimated cost in USD (based on the pricing tables above)
- Analysis mode (text, multimodal, filename_only)

This data is stored in the `verdicts` table in PostgreSQL (see doc 07). It enables:
- Comparing cost per file across providers
- Tracking total monthly AI spend
- Identifying which file types cost the most to analyze
- Making informed decisions about provider selection

**Note**: The pricing tables in the provider implementations should be configurable (loaded from config, not hardcoded) since API pricing changes frequently.

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `AI_PROVIDER` | Active provider ("anthropic", "openai", "gemini") | `anthropic` |
| `ANTHROPIC_API_KEY` | Anthropic API key | (required if provider is anthropic) |
| `ANTHROPIC_MODEL` | Anthropic model to use | `claude-sonnet-4-5-20250929` |
| `OPENAI_API_KEY` | OpenAI API key | (required if provider is openai) |
| `OPENAI_MODEL` | OpenAI model to use | `gpt-4o` |
| `GEMINI_API_KEY` | Google Gemini API key | (required if provider is gemini) |
| `GEMINI_MODEL` | Gemini model to use | `gemini-2.0-flash` |
| `AI_TEMPERATURE` | Temperature setting for AI calls | `0` |
| `AI_MAX_TOKENS` | Maximum output tokens | `1024` |
| `PROMPT_TEMPLATE_DIR` | Directory containing prompt templates | `config/prompt_templates` |

## Future: Local Model Support

The abstract interface is designed to accommodate a future local model provider. A local provider would implement the same `BaseAIProvider` interface but route requests to a locally hosted model (e.g., via Ollama, vLLM, or a similar local inference server) instead of a cloud API. The implementation would:

- Point to a local inference endpoint (e.g., `http://local-llm:8080/v1/chat/completions`)
- Report token counts from the local model's response
- Report cost as $0 (or a computed infrastructure cost if desired)
- Otherwise behave identically to the cloud providers

This requires no changes to the pipeline or orchestrator; only a new provider class and a new `ai_provider` configuration value.
