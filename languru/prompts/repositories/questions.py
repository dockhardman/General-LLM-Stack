from textwrap import dedent
from typing import Final, Text

question_of_co_star: Final[Text] = dedent(
    """
    The CO-STAR prompt framework is :

    **Context (C) :** Providing background information helps the LLM understand the specific scenario.

    **Objective (O):** Clearly defining the task directs the LLM’s focus.

    **Style (S):** Specifying the desired writing style aligns the LLM response.

    **Tone (T):** Setting the tone ensures the response resonates with the required sentiment.

    **Audience (A):** Identifying the intended audience tailors the LLM’s response to be targeted to an audience.

    **Response (R):** Providing the response format, like text or json, ensures the LLM outputs, and help build pipelines.

    Please explain it with gradually increasing complexity.
    """  # noqa: E501
).strip()
